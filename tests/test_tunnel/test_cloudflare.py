"""Tests for CloudflareProvider using a fake asyncio subprocess.

The provider's ``subprocess_factory`` kwarg is a test seam — we inject a
factory that returns a hand-rolled ``FakeProcess`` whose stdout we can
script line-by-line.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import hooky.tunnel.cloudflare as cloudflare_mod
from hooky.tunnel.base import TunnelError, TunnelStatus
from hooky.tunnel.cloudflare import MIN_CLOUDFLARED_VERSION, CloudflareProvider

_REAL_PROBE = cloudflare_mod._probe_version


def _probe_answering(output: str) -> Callable[[str], Awaitable[str]]:
    """A stand-in for ``_probe_version`` that prints ``output``."""

    async def _probe(binary: str) -> str:
        return output

    return _probe


@pytest.fixture(autouse=True)
def _compliant_cloudflared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs against a cloudflared that clears the version
    floor, unless it patches ``_probe_version`` itself (the floor tests do)."""
    monkeypatch.setattr(
        cloudflare_mod,
        "_probe_version",
        _probe_answering("cloudflared version 2026.10.4 (built 2026-10-26T02:34 UTC)\n"),
    )


class _FakeStream:
    """Async stdout stream that yields preloaded lines, then EOF.

    Tests can push additional lines via ``push()`` to simulate
    cloudflared emitting more output later (e.g. after start).
    """

    def __init__(self, initial: list[bytes]) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        for line in initial:
            self._queue.put_nowait(line)
        self._closed = False

    async def readline(self) -> bytes:
        if self._closed and self._queue.empty():
            return b""
        item = await self._queue.get()
        if item is None:  # EOF sentinel
            self._closed = True
            return b""
        return item

    def push(self, line: bytes) -> None:
        self._queue.put_nowait(line)

    def close(self) -> None:
        self._queue.put_nowait(None)


class _FakeProcess:
    """Minimal stand-in for ``asyncio.subprocess.Process``."""

    def __init__(self, stdout_lines: list[bytes], returncode_after_close: int = 0) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self._returncode: int | None = None
        self._returncode_after_close = returncode_after_close
        self._terminated = False
        self._killed = False
        self._wait_event = asyncio.Event()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._terminated = True
        # Closing stdout makes readline() return b"" -> watcher loop exits.
        self.stdout.close()
        self._returncode = self._returncode_after_close
        self._wait_event.set()

    def kill(self) -> None:
        self._killed = True
        self.stdout.close()
        self._returncode = -9
        self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        assert self._returncode is not None
        return self._returncode

    def simulate_exit(self, returncode: int) -> None:
        """Pretend the process died on its own (not via terminate)."""
        self.stdout.close()
        self._returncode = returncode
        self._wait_event.set()


def _factory_returning(process: _FakeProcess):
    """Return an async factory the provider can call to get a process."""

    async def _factory(argv: list[str]) -> Any:
        # argv unused in the fake — but verify shape for sanity:
        assert argv[0] == "cloudflared"
        assert "--token" in argv
        return process

    return _factory


# ----------------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------------


def test_empty_token_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty token"):
        CloudflareProvider(token="", hostname="x.example.com")


def test_empty_hostname_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty hostname"):
        CloudflareProvider(token="t", hostname="")


# ----------------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------------


async def test_start_returns_configured_public_url() -> None:
    """start() returns https://{hostname} once readiness marker is seen."""
    proc = _FakeProcess([b"some preamble\n", b"Connection registered\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="hooky.example.com",
        subprocess_factory=_factory_returning(proc),
    )
    url = await p.start(8000)
    assert url == "https://hooky.example.com"


async def test_start_recognises_alternate_marker() -> None:
    """Alternate cloudflared phrasing 'Registered tunnel connection' also signals ready."""
    proc = _FakeProcess([b"Registered tunnel connection\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        subprocess_factory=_factory_returning(proc),
    )
    assert (await p.start(8000)) == "https://h.example.com"


async def test_start_times_out_if_marker_never_appears() -> None:
    """If cloudflared keeps outputting unrelated lines, start() raises TunnelError."""
    proc = _FakeProcess([b"some unrelated output\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        startup_timeout=0.1,
        subprocess_factory=_factory_returning(proc),
    )
    with pytest.raises(TunnelError, match="Readiness marker not seen"):
        await p.start(8000)


async def test_start_fails_if_process_exits_immediately() -> None:
    """If cloudflared exits before readiness, surface as TunnelError."""
    proc = _FakeProcess([b"bad config\n"], returncode_after_close=1)
    # Simulate the process having already exited.
    proc.simulate_exit(1)
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        startup_timeout=1.0,
        subprocess_factory=_factory_returning(proc),
    )
    with pytest.raises(TunnelError):
        await p.start(8000)


async def test_start_is_idempotent() -> None:
    """Second start() with a live process returns same URL, doesn't respawn."""
    proc = _FakeProcess([b"Connection registered\n"])
    call_count = 0

    async def _factory(argv: list[str]) -> Any:
        nonlocal call_count
        call_count += 1
        return proc

    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        subprocess_factory=_factory,
    )
    url1 = await p.start(8000)
    url2 = await p.start(8000)
    assert url1 == url2
    assert call_count == 1


# ----------------------------------------------------------------------------
# Stop
# ----------------------------------------------------------------------------


async def test_stop_terminates_process() -> None:
    proc = _FakeProcess([b"Connection registered\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        subprocess_factory=_factory_returning(proc),
    )
    await p.start(8000)
    await p.stop()
    assert proc._terminated


async def test_stop_idempotent_on_never_started() -> None:
    proc = _FakeProcess([b"Connection registered\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        subprocess_factory=_factory_returning(proc),
    )
    await p.stop()  # must not raise
    assert not proc._terminated


# ----------------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------------


async def test_health_disconnected_before_start() -> None:
    proc = _FakeProcess([b"Connection registered\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        subprocess_factory=_factory_returning(proc),
    )
    h = await p.health()
    assert h.status is TunnelStatus.DISCONNECTED


async def test_health_connected_after_start() -> None:
    proc = _FakeProcess([b"Connection registered\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        subprocess_factory=_factory_returning(proc),
    )
    await p.start(8000)
    h = await p.health()
    assert h.status is TunnelStatus.CONNECTED
    assert h.public_url == "https://h.example.com"


async def test_health_error_after_start_failure() -> None:
    proc = _FakeProcess([b"noise\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        startup_timeout=0.1,
        subprocess_factory=_factory_returning(proc),
    )
    with pytest.raises(TunnelError):
        await p.start(8000)
    h = await p.health()
    assert h.status is TunnelStatus.ERROR
    assert h.error is not None


# ----------------------------------------------------------------------------
# Ops failure paths (the surfaces a real cloudflared subprocess exercises).
# These were previously deferred to the manual smoke test; covering them here
# so the smoke run starts from a known-good baseline.
# ----------------------------------------------------------------------------


class _RaisingStream:
    """stdout stand-in whose readline() raises — exercises the watcher's
    defensive except-clause (a broken pipe must not crash dispatch)."""

    async def readline(self) -> bytes:
        raise OSError("stream broke")


class _RaisingProcess:
    """Process whose output stream blows up on first read, then exits."""

    def __init__(self, returncode: int = 1) -> None:
        self.stdout = _RaisingStream()
        self._returncode: int | None = None
        self._final_rc = returncode
        self._event = asyncio.Event()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        self._returncode = self._final_rc
        self._event.set()
        return self._returncode

    def terminate(self) -> None:  # pragma: no cover - not reached in this test
        self._returncode = self._final_rc
        self._event.set()


class _StubbornProcess:
    """Ignores SIGTERM (terminate() doesn't exit), forcing SIGKILL escalation."""

    def __init__(self) -> None:
        self.stdout = _FakeStream([b"Connection registered\n"])
        self._returncode: int | None = None
        self._event = asyncio.Event()
        self.terminated = False
        self.killed = False

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True  # but deliberately does NOT exit

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        self.stdout.close()
        self._event.set()

    async def wait(self) -> int:
        await self._event.wait()
        assert self._returncode is not None
        return self._returncode


class _GhostProcess:
    """terminate()/kill() raise ProcessLookupError (already reaped), and the
    watcher never sees EOF — exercises both the swallow-ProcessLookupError
    branch and the watcher-join cancel path in stop()."""

    def __init__(self) -> None:
        self.stdout = _FakeStream([b"Connection registered\n"])
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        raise ProcessLookupError()

    def kill(self) -> None:  # pragma: no cover - not reached (terminate returns first)
        raise ProcessLookupError()

    async def wait(self) -> int:
        await asyncio.Event().wait()  # never resolves on its own
        raise AssertionError("unreachable")


async def test_start_missing_binary_raises_tunnelerror() -> None:
    """No subprocess_factory + a binary not on PATH -> actionable TunnelError."""
    p = CloudflareProvider(
        token="tok", hostname="h.example.com", binary="cloudflared-does-not-exist-xyz"
    )
    with pytest.raises(TunnelError, match="not found on PATH"):
        await p.start(8000)


async def test_start_wraps_unexpected_spawn_error() -> None:
    """A non-TunnelError raised while spawning is wrapped, and recorded in health."""

    async def _boom(argv: list[str]) -> Any:
        raise RuntimeError("kaboom")

    p = CloudflareProvider(token="tok", hostname="h.example.com", subprocess_factory=_boom)
    with pytest.raises(TunnelError, match="Failed to spawn cloudflared"):
        await p.start(8000)
    h = await p.health()
    assert h.status is TunnelStatus.ERROR
    assert "kaboom" in (h.error or "")


async def test_watcher_survives_stream_exception() -> None:
    """If the output stream raises, the watcher logs and still reaps the proc;
    start() surfaces the exit as a TunnelError and health reports ERROR."""
    proc = _RaisingProcess(returncode=1)
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        startup_timeout=1.0,
        subprocess_factory=_factory_returning(proc),
    )
    with pytest.raises(TunnelError):
        await p.start(8000)
    h = await p.health()
    assert h.status is TunnelStatus.ERROR


async def test_post_start_crash_marks_error_and_clears_url() -> None:
    """A tunnel that comes up cleanly then dies later flips health to ERROR
    and drops the public_url — so /healthz tells the truth without polling."""
    proc = _FakeProcess([b"Connection registered\n"])
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        subprocess_factory=_factory_returning(proc),
    )
    assert (await p.start(8000)) == "https://h.example.com"
    assert (await p.health()).status is TunnelStatus.CONNECTED

    proc.simulate_exit(1)  # cloudflared dies after a clean start
    await asyncio.sleep(0.01)  # let the watcher observe the exit

    h = await p.health()
    assert h.status is TunnelStatus.ERROR
    assert h.public_url is None

    await p.stop()  # stopping an already-dead tunnel is a clean no-op


async def test_stop_escalates_to_sigkill_when_sigterm_ignored() -> None:
    """If the process ignores SIGTERM, stop() escalates to SIGKILL after the
    grace period rather than hanging forever."""
    proc = _StubbornProcess()
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        terminate_grace=0.05,
        watcher_join_timeout=0.5,
        subprocess_factory=_factory_returning(proc),
    )
    await p.start(8000)
    await p.stop()
    assert proc.terminated
    assert proc.killed


async def test_stop_swallows_already_reaped_and_cancels_stuck_watcher() -> None:
    """terminate() raising ProcessLookupError (already gone) is swallowed, and
    a watcher that never drains is cancelled rather than blocking shutdown."""
    proc = _GhostProcess()
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        terminate_grace=0.05,
        watcher_join_timeout=0.1,
        subprocess_factory=_factory_returning(proc),
    )
    await p.start(8000)
    await p.stop()  # must not raise despite the reaped proc + stuck watcher


# ----------------------------------------------------------------------------
# Version floor: an old cloudflared trusts compromised eMudhra certificates,
# so start() refuses it before anything is spawned (fail closed).
# ----------------------------------------------------------------------------


def _never_spawned() -> Callable[[list[str]], Awaitable[Any]]:
    """A subprocess_factory that fails the test if the tunnel is ever spawned."""

    async def _factory(argv: list[str]) -> Any:
        raise AssertionError(f"cloudflared spawned despite the version floor: {argv}")

    return _factory


@pytest.mark.parametrize(
    "output",
    [
        "cloudflared version 2026.5.2 (built 2026-05-27T10:15 UTC)\n",
        "cloudflared version 2026.10.1 (built 2026-10-15T17:15 UTC)\n",
        # Numeric, not string, comparison: "2026.9.30" > "2026.10.2" as text.
        "cloudflared version 2026.9.30 (built 2026-09-30T00:00 UTC)\n",
    ],
)
async def test_start_refuses_cloudflared_below_floor(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    monkeypatch.setattr(cloudflare_mod, "_probe_version", _probe_answering(output))
    p = CloudflareProvider(
        token="tok", hostname="h.example.com", subprocess_factory=_never_spawned()
    )
    with pytest.raises(TunnelError, match="below the minimum"):
        await p.start(8000)
    h = await p.health()
    assert h.status is TunnelStatus.ERROR
    assert MIN_CLOUDFLARED_VERSION in (h.error or "")


async def test_start_refuses_unreadable_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev build or garbage output can't prove it's patched, so it's refused."""
    monkeypatch.setattr(
        cloudflare_mod, "_probe_version", _probe_answering("cloudflared version DEV\n")
    )
    p = CloudflareProvider(
        token="tok", hostname="h.example.com", subprocess_factory=_never_spawned()
    )
    with pytest.raises(TunnelError, match="Could not read a version"):
        await p.start(8000)
    assert (await p.health()).status is TunnelStatus.ERROR


async def test_start_accepts_cloudflared_at_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cloudflare_mod,
        "_probe_version",
        _probe_answering(f"cloudflared version {MIN_CLOUDFLARED_VERSION} (built x)\n"),
    )
    proc = _FakeProcess([b"Connection registered\n"])
    p = CloudflareProvider(
        token="tok", hostname="h.example.com", subprocess_factory=_factory_returning(proc)
    )
    assert (await p.start(8000)) == "https://h.example.com"


async def test_custom_min_version_raises_the_floor() -> None:
    """min_version above the default refuses the (otherwise fine) 2026.10.4."""
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        min_version="2027.1.0",
        subprocess_factory=_never_spawned(),
    )
    with pytest.raises(TunnelError, match="below the minimum 2027.1.0"):
        await p.start(8000)


def test_malformed_min_version_rejected() -> None:
    with pytest.raises(ValueError, match="min_version"):
        CloudflareProvider(token="t", hostname="h.example.com", min_version="latest")


async def test_probe_missing_binary_raises_tunnelerror() -> None:
    with pytest.raises(TunnelError, match="not found on PATH"):
        await _REAL_PROBE("cloudflared-does-not-exist-xyz")


async def test_real_probe_refuses_a_binary_that_is_not_cloudflared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through a real subprocess: ``python --version`` says
    "Python 3.x", which is no proof of a patched cloudflared."""
    monkeypatch.setattr(cloudflare_mod, "_probe_version", _REAL_PROBE)
    p = CloudflareProvider(
        token="tok",
        hostname="h.example.com",
        binary=sys.executable,
        subprocess_factory=_never_spawned(),
    )
    with pytest.raises(TunnelError, match="Could not read a version"):
        await p.start(8000)
