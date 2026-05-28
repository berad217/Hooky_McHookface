"""The plugin-route seam: handlers may contribute auxiliary APIRouters.

A handler can optionally implement :meth:`WebhookHandler.routers`; the app
factory collects and mounts them alongside the webhook receiver. The seam is
isolated like dispatch — a handler that raises while producing routers must not
prevent the app (and webhook ingest) from coming up.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.testclient import TestClient

from hooky.app import create_app
from hooky.handlers.base import WebhookHandler
from hooky.handlers.manager import HandlerManager
from hooky.storage.models import StoredWebhook
from hooky.storage.sqlite import AsyncSQLiteStorage


class _RouterHandler(WebhookHandler):
    """A router-only provider: inert ``process``, contributes one GET route."""

    @property
    def name(self) -> str:
        return "router_provider"

    async def process(self, webhook: StoredWebhook) -> Any:
        return None

    def routers(self) -> list[APIRouter]:
        router = APIRouter()

        @router.get("/aux/ping")
        async def ping() -> dict[str, str]:
            return {"aux": "pong"}

        return [router]


class _PlainHandler(WebhookHandler):
    """A normal handler that contributes no routers (default behavior)."""

    @property
    def name(self) -> str:
        return "plain"

    async def process(self, webhook: StoredWebhook) -> Any:
        return None


class _BrokenRoutersHandler(WebhookHandler):
    """A handler whose ``routers()`` raises — must not sink the app."""

    @property
    def name(self) -> str:
        return "broken_routers"

    async def process(self, webhook: StoredWebhook) -> Any:
        return None

    def routers(self) -> list[APIRouter]:
        raise RuntimeError("boom while building routers")


def _app_with(handlers: list[WebhookHandler], storage: AsyncSQLiteStorage) -> TestClient:
    manager = HandlerManager(storage=storage)
    for h in handlers:
        manager.register(h)
    app = create_app(storage=storage, handler_manager=manager)
    return TestClient(app)


def test_contributed_router_is_mounted(storage: AsyncSQLiteStorage) -> None:
    """A handler's contributed route is served by the app."""
    with _app_with([_RouterHandler()], storage) as client:
        resp = client.get("/aux/ping")
    assert resp.status_code == 200
    assert resp.json() == {"aux": "pong"}


def test_default_handler_contributes_nothing(storage: AsyncSQLiteStorage) -> None:
    """A handler that doesn't override routers() mounts no extra routes; the core
    webhook + healthz routes still work."""
    with _app_with([_PlainHandler()], storage) as client:
        assert client.get("/aux/ping").status_code == 404  # nothing mounted
        assert client.get("/healthz").status_code == 200  # core route intact


def test_broken_routers_does_not_sink_the_app(storage: AsyncSQLiteStorage) -> None:
    """A handler raising from routers() is logged + skipped: the app still comes
    up, webhook ingest works, and a *sibling* handler's routers still mount."""
    with _app_with([_BrokenRoutersHandler(), _RouterHandler()], storage) as client:
        # The broken handler didn't prevent startup or the sibling's route.
        assert client.get("/aux/ping").json() == {"aux": "pong"}
        # And the core webhook path is alive (durability invariant).
        resp = client.post("/webhook/test", json={"hello": "world"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
