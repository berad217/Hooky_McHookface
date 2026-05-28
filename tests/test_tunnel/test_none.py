"""Tests for NoTunnelProvider."""

from __future__ import annotations

from hooky.tunnel.base import TunnelStatus
from hooky.tunnel.none_provider import NoTunnelProvider


async def test_start_returns_localhost_url() -> None:
    """start(port) returns http://localhost:{port}."""
    p = NoTunnelProvider()
    url = await p.start(8000)
    assert url == "http://localhost:8000"


async def test_start_is_idempotent() -> None:
    """start() twice returns the same URL."""
    p = NoTunnelProvider()
    url1 = await p.start(8000)
    url2 = await p.start(8000)
    assert url1 == url2


async def test_custom_host() -> None:
    """host kwarg flows through."""
    p = NoTunnelProvider(host="127.0.0.1")
    assert await p.start(1234) == "http://127.0.0.1:1234"


async def test_health_disconnected_before_start() -> None:
    """Before start, health is DISCONNECTED."""
    p = NoTunnelProvider()
    h = await p.health()
    assert h.status is TunnelStatus.DISCONNECTED
    assert h.public_url is None


async def test_health_connected_after_start() -> None:
    """After start, health is CONNECTED with the URL."""
    p = NoTunnelProvider()
    await p.start(8000)
    h = await p.health()
    assert h.status is TunnelStatus.CONNECTED
    assert h.public_url == "http://localhost:8000"


async def test_stop_resets_state() -> None:
    """stop() returns us to DISCONNECTED; start() afterwards still works."""
    p = NoTunnelProvider()
    await p.start(8000)
    await p.stop()
    h = await p.health()
    assert h.status is TunnelStatus.DISCONNECTED
    # Re-start with a different port still works.
    url = await p.start(9000)
    assert url == "http://localhost:9000"


async def test_stop_idempotent_on_never_started() -> None:
    """stop() on a fresh provider is a no-op (no exception)."""
    p = NoTunnelProvider()
    await p.stop()  # must not raise
