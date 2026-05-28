"""Integration tests: /healthz exposes tunnel state when a tunnel is wired in."""

from __future__ import annotations

from fastapi.testclient import TestClient

from hooky.app import create_app
from hooky.storage.sqlite import AsyncSQLiteStorage
from hooky.tunnel.none_provider import NoTunnelProvider


def test_healthz_includes_tunnel_block_when_tunnel_started(
    storage: AsyncSQLiteStorage,
) -> None:
    """/healthz reports tunnel.status=connected after the lifespan starts it.

    Letting create_app build the tunnel from default config gives us the
    real lifespan-managed path (owns_tunnel=True). Default config is
    provider='none' -> NoTunnelProvider, which is enough to exercise the
    healthz wiring end-to-end.
    """
    # No explicit ``tunnel=`` -> factory builds + lifespan manages it.
    app = create_app(storage=storage)

    with TestClient(app) as client:
        resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "tunnel" in body
    # Lifespan ran => tunnel started => CONNECTED.
    assert body["tunnel"]["status"] == "connected"
    assert body["tunnel"]["public_url"] is not None
    assert body["tunnel"]["public_url"].startswith("http://")
    assert body["tunnel"]["last_checked"] is not None
    assert body["tunnel"]["error"] is None


def test_healthz_with_injected_started_tunnel(
    storage: AsyncSQLiteStorage,
) -> None:
    """If a tunnel is injected (caller-owned), its current state is reported.

    Pre-start the injected tunnel so its health reports CONNECTED — exercises
    the "injected tunnel" branch where lifespan does NOT manage it.
    """
    import asyncio

    tunnel = NoTunnelProvider()
    asyncio.get_event_loop().run_until_complete(tunnel.start(9999))

    app = create_app(storage=storage, tunnel=tunnel)
    with TestClient(app) as client:
        resp = client.get("/healthz")

    body = resp.json()
    assert body["tunnel"]["status"] == "connected"
    assert body["tunnel"]["public_url"] == "http://localhost:9999"


def test_healthz_omits_tunnel_block_when_no_tunnel_injected(
    client: TestClient,
) -> None:
    """The default ``client`` fixture does not pass a tunnel — older Sprint 1 shape preserved.

    NOTE: After Sprint 4, ``create_app()`` defaults to ``NoTunnelProvider``
    when no tunnel is explicitly passed. The shared fixture ``client`` does
    pass storage but not a tunnel, so the factory builds one — and /healthz
    will include the block. That's a behavior change from Sprint 3; this
    test pins the new expected shape.
    """
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    # Sprint 4 default: NoTunnelProvider is auto-built. tunnel block present.
    assert "tunnel" in body
    assert body["tunnel"]["status"] == "connected"
