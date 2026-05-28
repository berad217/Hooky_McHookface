"""Cloudflare Tunnel provider — spawns ``cloudflared`` as a subprocess.

Brand new in Hooky_McHookface v1 (the old ``webhook-reciever`` codebase
had a stub for this but never built it). The design:

- ``start()`` launches ``cloudflared tunnel run --token <token>`` as an
  asyncio subprocess, pipes its stderr/stdout into a background task
  that scans for the "Connection registered" marker that cloudflared
  prints once the tunnel is live.
- The public hostname is **configured**, not discovered. Cloudflare
  Tunnels are pre-provisioned on the Cloudflare side (`hooky-main.example.com`
  is set up in the dashboard); cloudflared itself never prints the
  hostname. So we take it from config and trust it.
- ``stop()`` sends SIGTERM, waits briefly, escalates to SIGKILL if needed.
  Idempotent.
- ``health()`` returns cached state — connected once we saw the marker,
  error if the process exited unexpectedly, disconnected before/after.

A subprocess-watcher task observes the process and updates the cached
state if cloudflared crashes after start. That way ``/healthz`` reports
the truth without needing to poll.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from collections.abc import Mapping
from typing import Any, cast

from hooky.log import get_logger
from hooky.tunnel.base import (
    TunnelError,
    TunnelHealth,
    TunnelProvider,
    TunnelStatus,
    _utcnow,
)

logger = get_logger(__name__)

# Marker cloudflared writes to its log stream once the tunnel is live. Both
# canonical phrasings are checked because the exact wording has varied
# across cloudflared releases.
_READY_MARKERS = (b"Connection registered", b"Registered tunnel connection")

# Bounds on how long we'll wait for the readiness marker before giving up.
_DEFAULT_STARTUP_TIMEOUT = 30.0


class CloudflareProvider(TunnelProvider):
    """Run ``cloudflared`` as a managed subprocess.

    Args:
        token: Cloudflare Tunnel token (the ``--token`` value, NOT the
            Cloudflare API token). Empty strings are rejected.
        hostname: The public hostname this tunnel is configured to serve
            (e.g. ``"hooky-main.example.com"``). Set up on the Cloudflare
            dashboard, repeated here for our records — cloudflared
            itself never reports it back over its log stream.
        binary: Path to the ``cloudflared`` binary. Defaults to whatever
            is on ``$PATH``. Tests inject a stub.
        startup_timeout: Seconds to wait for the readiness marker before
            considering startup failed. Default 30s.
        terminate_grace: Seconds to wait after SIGTERM before escalating to
            SIGKILL during teardown. Default 5s. Exposed as a seam so tests
            can drive the escalation path without a real wall-clock wait.
        watcher_join_timeout: Seconds ``stop()`` waits for the output-watcher
            task to drain before cancelling it. Default 2s. Test seam.
        subprocess_factory: Optional async callable
            ``async (argv: list[str]) -> asyncio.subprocess.Process``
            used to spawn the subprocess. Tests override this. Production
            uses ``asyncio.create_subprocess_exec``.

    Raises:
        ValueError: If ``token`` or ``hostname`` is empty.
    """

    def __init__(
        self,
        token: str,
        hostname: str,
        *,
        binary: str = "cloudflared",
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
        terminate_grace: float = 5.0,
        watcher_join_timeout: float = 2.0,
        subprocess_factory: Any = None,
    ) -> None:
        if not token:
            raise ValueError("CloudflareProvider requires a non-empty token.")
        if not hostname:
            raise ValueError("CloudflareProvider requires a non-empty hostname.")
        self._token = token
        self._hostname = hostname
        self._binary = binary
        self._startup_timeout = startup_timeout
        self._terminate_grace = terminate_grace
        self._watcher_join_timeout = watcher_join_timeout
        self._subprocess_factory = subprocess_factory

        self._proc: asyncio.subprocess.Process | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._url: str | None = None
        self._last_error: str | None = None

    def _public_url(self) -> str:
        """Construct the public URL from the configured hostname."""
        return f"https://{self._hostname}"

    async def _spawn(self) -> asyncio.subprocess.Process:
        """Spawn the cloudflared process. Test seam."""
        argv = [self._binary, "tunnel", "run", "--token", self._token]
        if self._subprocess_factory is not None:
            return cast("asyncio.subprocess.Process", await self._subprocess_factory(argv))

        if shutil.which(self._binary) is None:
            raise TunnelError(
                f"'{self._binary}' not found on PATH. Install cloudflared from "
                "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/."
            )
        return await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def start(self, port: int) -> str:
        """Launch cloudflared, wait for the readiness marker, return public URL.

        ``port`` is accepted to satisfy the ABC contract but is NOT passed
        to cloudflared — Cloudflare Tunnels use the configuration stored
        with the token on Cloudflare's side. The tunnel routes traffic to
        whatever ingress is configured in the dashboard.

        Args:
            port: Ignored. Present for ABC compatibility.

        Returns:
            ``"https://{hostname}"``.

        Raises:
            TunnelError: If cloudflared can't be spawned, exits before the
                readiness marker, or the marker isn't seen within
                ``startup_timeout``.
        """
        del port  # accepted but not used; Cloudflare Tunnels are pre-routed

        if self._url is not None and self._proc is not None and self._proc.returncode is None:
            return self._url

        # Fresh start: clear stale state from a previous failed attempt.
        self._ready = asyncio.Event()
        self._last_error = None

        try:
            proc = await self._spawn()
        except TunnelError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap unexpected spawn errors
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise TunnelError(f"Failed to spawn cloudflared: {exc}") from exc

        self._proc = proc
        self._watcher_task = asyncio.create_task(self._watch_output())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._startup_timeout)
        except TimeoutError as exc:
            await self._terminate_proc()
            self._last_error = (
                f"Readiness marker not seen within {self._startup_timeout}s."
            )
            raise TunnelError(self._last_error) from exc

        # If the watcher set _ready=True but the process has *also* exited,
        # treat that as a startup failure rather than success.
        if self._proc is None or self._proc.returncode is not None:
            rc_repr = self._proc.returncode if self._proc else "unknown"
            self._last_error = self._last_error or f"cloudflared exited with code {rc_repr}."
            raise TunnelError(self._last_error)

        self._url = self._public_url()
        logger.info(
            "cloudflare_tunnel_started",
            public_url=self._url,
            hostname=self._hostname,
        )
        return self._url

    async def _watch_output(self) -> None:
        """Background task: read cloudflared's combined output line-by-line.

        Sets ``_ready`` when a readiness marker is seen. Sets ``_last_error``
        and clears ``_url`` if the process exits unexpectedly.
        """
        assert self._proc is not None
        proc = self._proc
        assert proc.stdout is not None  # we always pipe stdout

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                if not self._ready.is_set():
                    for marker in _READY_MARKERS:
                        if marker in line:
                            self._ready.set()
                            break
                # Log every line at debug — useful for ops, costs nothing.
                logger.debug("cloudflared_stdout", line=line.decode("utf-8", "replace").rstrip())
        except Exception as exc:  # noqa: BLE001 — never let the watcher crash dispatch
            logger.error("cloudflared_watcher_crashed", error=str(exc))

        # Process has exited (or stdout closed).
        rc = await proc.wait()
        if rc != 0:
            self._last_error = f"cloudflared exited with code {rc}."
            logger.error("cloudflared_exited", returncode=rc)
        else:
            logger.info("cloudflared_exited", returncode=rc)
        # Unblock any pending start() that's still waiting on _ready —
        # wait_for will see _ready set + proc dead and treat it as failure.
        self._ready.set()
        # Mark tunnel as no longer connected.
        self._url = None

    async def _terminate_proc(self) -> None:
        """Send SIGTERM, then SIGKILL after a short grace period. Idempotent."""
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is not None:
            return  # already exited

        try:
            # Windows asyncio subprocess does not implement send_signal(SIGTERM)
            # the same way as POSIX; use terminate() which maps to TerminateProcess.
            proc.terminate()
        except ProcessLookupError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloudflared_terminate_failed", error=str(exc))

        try:
            await asyncio.wait_for(proc.wait(), timeout=self._terminate_grace)
        except TimeoutError:
            logger.warning("cloudflared_terminate_grace_expired_killing")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("cloudflared_kill_failed", error=str(exc))
            # Best effort to await; ignore further timeouts.
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        """Tear down cloudflared. Idempotent.

        Sends SIGTERM and waits up to 5s, then SIGKILL. The watcher task
        is awaited so we don't leak a pending coroutine.
        """
        if self._proc is None:
            return

        await self._terminate_proc()
        if self._watcher_task is not None:
            try:
                await asyncio.wait_for(self._watcher_task, timeout=self._watcher_join_timeout)
            except TimeoutError:
                self._watcher_task.cancel()

        self._proc = None
        self._watcher_task = None
        self._url = None
        logger.info("cloudflare_tunnel_stopped")

    async def health(self) -> TunnelHealth:
        """Return cached health state. No process polling.

        The watcher task keeps state fresh; ``health()`` is a pure read.
        """
        # The watcher updates _url to None if the process died, so a live
        # _url with a still-running proc == CONNECTED.
        if self._url is not None and self._proc is not None and self._proc.returncode is None:
            return TunnelHealth(
                status=TunnelStatus.CONNECTED,
                public_url=self._url,
                last_checked=_utcnow(),
            )
        if self._last_error is not None:
            return TunnelHealth(
                status=TunnelStatus.ERROR,
                public_url=None,
                last_checked=_utcnow(),
                error=self._last_error,
            )
        return TunnelHealth(
            status=TunnelStatus.DISCONNECTED,
            public_url=None,
            last_checked=_utcnow(),
        )


def _build_from_config(
    cfg: Mapping[str, Any], *, env: Mapping[str, str]
) -> CloudflareProvider:
    """Construct a :class:`CloudflareProvider` from the ``tunnel.cloudflare`` block.

    Args:
        cfg: The ``cloudflare:`` sub-block (``tunnel_name`` /
            ``hostname``).
        env: Environment mapping. ``CLOUDFLARED_TOKEN`` must be set.

    Returns:
        Configured :class:`CloudflareProvider`.

    Raises:
        TunnelError: If ``CLOUDFLARED_TOKEN`` is missing, or required
            config fields are absent.
    """
    token = env.get("CLOUDFLARED_TOKEN", "")
    if not token:
        raise TunnelError(
            "Tunnel provider 'cloudflare' requires env var CLOUDFLARED_TOKEN to be set."
        )
    hostname = cfg.get("hostname") or cfg.get("tunnel_name")
    if not hostname:
        raise TunnelError(
            "Tunnel provider 'cloudflare' requires 'hostname' (or legacy 'tunnel_name') in config."
        )
    return CloudflareProvider(token=token, hostname=hostname)


# Silence unused-import on POSIX-only signal module on Windows builds.
_ = (os, signal)
