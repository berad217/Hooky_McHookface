"""Hooky CLI — operate a webhook receiver from the terminal.

Commands:

- ``hooky start [--config PATH]``  — run the server (+ tunnel) in the foreground.
- ``hooky stop [--pidfile PATH]``  — stop a server started with ``start``.
- ``hooky list [--limit N] [--path P]``  — recent webhooks as a table.
- ``hooky show <id>``  — full detail for one webhook, incl. handler runs.
- ``hooky replay <id> [--handler NAME]...``  — re-dispatch a stored webhook.

Async note: storage is async, but the CLI is an *application entry point*,
not framework-internal code — so using ``asyncio.run`` here does not
violate the "no asyncio.run inside the framework" rule. The framework
modules stay pure-async; the CLI is the boundary where we cross into a
synchronous Click world.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import click

from hooky.config import HookyConfig, load_config
from hooky.handlers.registry import build_handler_manager
from hooky.replay.engine import ReplayError, replay_webhook
from hooky.storage.models import HandlerResult, StoredWebhook
from hooky.storage.sqlite import AsyncSQLiteStorage

_DEFAULT_PIDFILE = "data/hooky.pid"

# Module-level platform flag (not a direct ``os.name`` read at the call sites)
# so tests can drive both the POSIX and Windows ``stop`` branches deterministically
# by patching this one symbol — without mutating the real ``os.name``, which would
# also change ``pathlib`` flavour and break path handling mid-test.
_IS_WINDOWS = os.name == "nt"

# ``taskkill`` exit code when no process matches the given PID. Used to map a
# vanished process onto the same stale-pidfile cleanup path that POSIX's
# ``ProcessLookupError`` triggers.
_TASKKILL_NOT_FOUND = 128

# Bind-retry tuning for the Windows exclusive-socket path (see
# ``_bind_exclusive_socket``). A handful of short retries absorbs the brief
# window on a fast service restart where the previous process has not yet fully
# released the port; long enough to ride out a normal ``nssm restart``, short
# enough that a genuinely-held port still fails the start within a few seconds.
_BIND_ATTEMPTS = 5
_BIND_BACKOFF_S = 0.5


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _terminate_server(pid: int) -> None:
    """Stop the running ``hooky start`` server, reaping its child processes too.

    Why this is platform-split — the cloudflared-orphan bug:

    The webhook server spawns ``cloudflared`` as a managed child subprocess.
    Reaping that child is the job of the FastAPI ``lifespan`` teardown, which
    calls the tunnel provider's ``stop()`` — but that teardown only runs if the
    server process shuts down *gracefully*.

    - **POSIX:** ``SIGTERM`` triggers uvicorn's graceful shutdown, so the
      lifespan teardown runs and ``cloudflared`` is reaped. A single signal
      suffices. This is also the production path (``docker compose down`` sends
      ``SIGTERM`` to uvicorn as PID 1) — left unchanged so we don't regress it.
    - **Windows:** there are no POSIX signals; ``os.kill(pid, SIGTERM)`` maps to
      ``TerminateProcess``, which hard-kills uvicorn *without* running its
      shutdown handlers. The lifespan teardown never executes, so the
      ``cloudflared`` child is orphaned. We instead kill the whole process tree
      with ``taskkill /T`` so the child is reaped along with its parent.

    Args:
        pid: PID of the server process read from the pidfile.

    Raises:
        ProcessLookupError: No process with ``pid`` exists (stale pidfile).
        OSError: The platform termination call failed for another reason.
    """
    if _IS_WINDOWS:
        # /T = terminate this process tree (children too), /F = force.
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return
        if completed.returncode == _TASKKILL_NOT_FOUND:
            raise ProcessLookupError(pid)
        raise OSError(
            (completed.stderr or completed.stdout).strip()
            or f"taskkill exited with code {completed.returncode}"
        )

    # POSIX: graceful SIGTERM -> uvicorn shutdown -> lifespan -> tunnel.stop().
    os.kill(pid, signal.SIGTERM)


def _bind_exclusive_socket(
    host: str,
    port: int,
    *,
    attempts: int = _BIND_ATTEMPTS,
    backoff_s: float = _BIND_BACKOFF_S,
) -> socket.socket:
    """Bind a listening socket that *exclusively* owns ``host:port`` (Windows).

    Why this exists — the port-shadow incident:

    On Windows, a second process that sets ``SO_REUSEADDR`` is allowed to bind a
    port another process is already actively listening on — silently co-binding
    and stealing inbound traffic — *unless* the original socket claimed the port
    with ``SO_EXCLUSIVEADDRUSE``. uvicorn's default socket does the opposite (it
    sets ``SO_REUSEADDR`` and not the exclusive flag), so a stray
    ``python -m http.server <port>`` once shadowed a live receiver for two days
    with neither side erroring. Claiming the port exclusively here inverts that:
    the *newcomer's* bind fails loudly, and the running server is never displaced.

    The socket is returned bound but un-``listen``ed; uvicorn (via asyncio's
    ``create_server``) calls ``listen`` when we hand the socket over. A short
    bind-retry loop absorbs the race on a fast restart, where the previous
    process may not have released the port yet.

    Args:
        host: Bind host (e.g. ``"0.0.0.0"`` or ``"127.0.0.1"``).
        port: Bind port (``0`` lets the OS pick a free one).
        attempts: Maximum bind attempts before giving up.
        backoff_s: Seconds to sleep between attempts.

    Returns:
        A bound, inheritable socket ready to pass to uvicorn via ``sockets=``.

    Raises:
        OSError: The port could not be claimed exclusively within ``attempts``.
    """
    # ``SO_EXCLUSIVEADDRUSE`` is a Windows-only socket option; this function is
    # only ever reached on the Windows branch of ``_run_server`` (and the tooling
    # type-checks on Windows, where the stub defines it).
    exclusive_opt = socket.SO_EXCLUSIVEADDRUSE

    # Resolve the address family from the host so an IPv6 bind host still works.
    family, socktype, proto, _canon, sockaddr = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )[0]

    last_err: OSError | None = None
    for attempt in range(attempts):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, exclusive_opt, 1)
            sock.bind(sockaddr)
            sock.set_inheritable(True)
            return sock
        except OSError as exc:
            sock.close()
            last_err = exc
            if attempt + 1 < attempts:
                time.sleep(backoff_s)

    raise OSError(
        f"could not exclusively bind {host}:{port} after {attempts} attempt(s); "
        f"another process is holding the port: {last_err}"
    )


def _run_server(app: Any, host: str, port: int) -> None:
    """Run the uvicorn server, claiming the port exclusively on Windows.

    Platform split — only Windows has the silent-shadow footgun described in
    :func:`_bind_exclusive_socket`:

    - **Windows:** pre-bind an exclusive socket and hand it to uvicorn (which then
      skips its own ``SO_REUSEADDR`` bind), so a later stray binder fails loudly.
    - **POSIX:** ``SO_REUSEADDR`` does not let a second process steal an actively
      listening socket, and the production ``docker``/``SIGTERM`` shutdown path
      depends on uvicorn owning the socket lifecycle — so leave it untouched.

    Args:
        app: The ASGI application returned by ``create_app``.
        host: Bind host.
        port: Bind port.
    """
    import uvicorn

    if not _IS_WINDOWS:
        uvicorn.run(app, host=host, port=port, log_config=None)
        return

    sock = _bind_exclusive_socket(host, port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_config=None))
    server.run(sockets=[sock])


def _resolve_config(config: str | None, db: str | None) -> HookyConfig:
    """Load config (or defaults) and apply a ``--db`` override.

    Args:
        config: Path to a YAML config file, or ``None`` for defaults.
        db: Optional storage path override (CLI ``--db`` beats config).

    Returns:
        The resolved :class:`HookyConfig`.
    """
    cfg = load_config(config) if config else HookyConfig()
    if db is not None:
        cfg = cfg.model_copy(update={"storage": cfg.storage.model_copy(update={"path": db})})
    return cfg


async def _open_storage(cfg: HookyConfig) -> AsyncSQLiteStorage:
    """Construct + connect an :class:`AsyncSQLiteStorage` from config."""
    storage = AsyncSQLiteStorage(cfg.storage.path)
    await storage.connect()
    return storage


def _fmt_dt(value: Any) -> str:
    """Format a datetime to a compact ISO-ish string for tables."""
    try:
        return str(value.strftime("%Y-%m-%d %H:%M:%S"))
    except AttributeError:
        return str(value)


def _sig_label(stored: StoredWebhook) -> str:
    """Human label for a webhook's signature status."""
    if stored.signature_valid is None:
        return "-"
    return "valid" if stored.signature_valid else "INVALID"


# --------------------------------------------------------------------------
# CLI group
# --------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="hooky-mchookface", prog_name="hooky")
def cli() -> None:
    """Hooky_McHookface — async-native webhook receiver."""


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


@cli.command(name="list")
@click.option("--config", "config_path", type=click.Path(), default=None, help="Config YAML path.")
@click.option("--db", default=None, help="SQLite DB path (overrides config).")
@click.option("--limit", type=int, default=50, show_default=True, help="Max rows to show.")
@click.option("--path", "path_filter", default=None, help="Filter by exact webhook path.")
def list_cmd(config_path: str | None, db: str | None, limit: int, path_filter: str | None) -> None:
    """List recent stored webhooks, most recent first."""
    cfg = _resolve_config(config_path, db)

    async def _run() -> list[StoredWebhook]:
        storage = await _open_storage(cfg)
        try:
            return await storage.list(limit=limit, path=path_filter)
        finally:
            await storage.close()

    rows = asyncio.run(_run())

    if not rows:
        click.echo("No webhooks stored.")
        return

    header = f"{'ID':<26}  {'RECEIVED':<19}  {'PATH':<24}  {'SIG':<8}"
    click.echo(header)
    click.echo("-" * len(header))
    for w in rows:
        click.echo(
            f"{w.id:<26}  {_fmt_dt(w.received_at):<19}  {w.path:<24}  {_sig_label(w):<8}"
        )
    click.echo(f"\n{len(rows)} webhook(s).")


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------


@cli.command()
@click.argument("webhook_id")
@click.option("--config", "config_path", type=click.Path(), default=None, help="Config YAML path.")
@click.option("--db", default=None, help="SQLite DB path (overrides config).")
def show(webhook_id: str, config_path: str | None, db: str | None) -> None:
    """Show full detail for a single webhook, including handler runs."""
    cfg = _resolve_config(config_path, db)

    async def _run() -> tuple[StoredWebhook | None, list[HandlerResult]]:
        storage = await _open_storage(cfg)
        try:
            stored = await storage.get(webhook_id)
            if stored is None:
                return None, []
            runs = await storage.get_handler_runs(webhook_id)
            return stored, runs
        finally:
            await storage.close()

    stored, runs = asyncio.run(_run())

    if stored is None:
        click.echo(f"No webhook with id {webhook_id!r}.", err=True)
        raise SystemExit(1)

    click.echo(f"ID:            {stored.id}")
    click.echo(f"Path:          {stored.path}")
    click.echo(f"Method:        {stored.method}")
    click.echo(f"Received at:   {stored.received_at.isoformat()}")
    click.echo(f"Source IP:     {stored.source_ip or '-'}")
    click.echo(f"Signature:     {_sig_label(stored)}", nl=True)
    if stored.signature_validator:
        click.echo(f"Validator:     {stored.signature_validator}")

    click.echo("\nHeaders:")
    for key, value in sorted(stored.headers.items()):
        click.echo(f"  {key}: {value}")

    click.echo("\nBody:")
    if stored.body_json is not None:
        click.echo(json.dumps(stored.body_json, indent=2))
    else:
        try:
            click.echo(stored.body.decode("utf-8"))
        except UnicodeDecodeError:
            click.echo(f"<{len(stored.body)} bytes of binary data>")

    click.echo("\nHandler runs:")
    if not runs:
        click.echo("  (none)")
    else:
        for r in runs:
            status = "OK " if r.success else "ERR"
            latency = (r.completed_at - r.started_at).total_seconds() * 1000.0
            click.echo(
                f"  [{status}] {r.handler_name} "
                f"({latency:.1f}ms, at {_fmt_dt(r.started_at)})"
            )
            if not r.success and r.error:
                click.echo(f"        error: {r.error}")


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


@cli.command()
@click.argument("webhook_id")
@click.option("--config", "config_path", type=click.Path(), default=None, help="Config YAML path.")
@click.option("--db", default=None, help="SQLite DB path (overrides config).")
@click.option(
    "--handler",
    "handler_names",
    multiple=True,
    help="Only replay to this handler (repeatable). Omit for all handlers.",
)
def replay(
    webhook_id: str,
    config_path: str | None,
    db: str | None,
    handler_names: tuple[str, ...],
) -> None:
    """Re-dispatch a stored webhook through configured handlers."""
    cfg = _resolve_config(config_path, db)
    names = list(handler_names) if handler_names else None

    async def _run() -> Any:
        storage = await _open_storage(cfg)
        try:
            manager = build_handler_manager(cfg.handlers, storage=storage)
            if not manager.handlers:
                click.echo(
                    "No handlers configured. Add a 'handlers:' section to your "
                    "config (dotted import paths) so replay has something to "
                    "dispatch to.",
                    err=True,
                )
                raise SystemExit(2)
            return await replay_webhook(storage, manager, webhook_id, names)
        finally:
            await storage.close()

    try:
        result = asyncio.run(_run())
    except ReplayError as exc:
        click.echo(f"Replay failed: {exc}", err=True)
        raise SystemExit(1) from exc

    click.echo(f"Replayed {result.webhook.id} ({result.webhook.path})")
    click.echo(f"Dispatched to {len(result.results)} handler(s):\n")
    for r in result.results:
        status = "OK " if r.success else "ERR"
        latency = (r.completed_at - r.started_at).total_seconds() * 1000.0
        click.echo(f"  [{status}] {r.handler_name} ({latency:.1f}ms)")
        if not r.success and r.error:
            click.echo(f"        error: {r.error}")


# --------------------------------------------------------------------------
# start / stop
# --------------------------------------------------------------------------


@cli.command()
@click.option("--config", "config_path", type=click.Path(), default=None, help="Config YAML path.")
@click.option("--db", default=None, help="SQLite DB path (overrides config).")
@click.option("--host", default=None, help="Bind host (overrides config).")
@click.option("--port", type=int, default=None, help="Bind port (overrides config).")
@click.option("--pidfile", default=_DEFAULT_PIDFILE, show_default=True, help="PID file path.")
def start(
    config_path: str | None,
    db: str | None,
    host: str | None,
    port: int | None,
    pidfile: str,
) -> None:
    """Start the webhook receiver server (foreground). Ctrl-C to stop."""
    from hooky.app import create_app

    cfg = _resolve_config(config_path, db)
    bind_host = host if host is not None else cfg.server.host
    bind_port = port if port is not None else cfg.server.port

    app = create_app(config=cfg)

    pid_path = Path(pidfile)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    click.echo(f"hooky starting on {bind_host}:{bind_port} (pid {os.getpid()}, pidfile {pidfile})")

    try:
        _run_server(app, bind_host, bind_port)
    finally:
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass


@cli.command()
@click.option("--pidfile", default=_DEFAULT_PIDFILE, show_default=True, help="PID file path.")
def stop(pidfile: str) -> None:
    """Stop a server previously started with ``hooky start``."""
    pid_path = Path(pidfile)
    if not pid_path.is_file():
        click.echo(f"No pidfile at {pidfile}; is the server running?", err=True)
        raise SystemExit(1)

    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as exc:
        click.echo(f"Could not read pid from {pidfile}: {exc}", err=True)
        raise SystemExit(1) from exc

    try:
        _terminate_server(pid)
    except ProcessLookupError:
        click.echo(f"No process with pid {pid}; cleaning up stale pidfile.")
        pid_path.unlink(missing_ok=True)
        return
    except OSError as exc:
        click.echo(f"Failed to signal pid {pid}: {exc}", err=True)
        raise SystemExit(1) from exc

    if _IS_WINDOWS:
        # The Windows tree-kill is a hard termination, so `start`'s own pidfile
        # cleanup (its `finally`) never runs — remove the pidfile here. On POSIX
        # the graceful shutdown lets `start` clean up its own pidfile.
        pid_path.unlink(missing_ok=True)
        click.echo(f"Stopped pid {pid} and its child processes.")
    else:
        click.echo(f"Sent stop signal to pid {pid}.")


if __name__ == "__main__":
    cli()
