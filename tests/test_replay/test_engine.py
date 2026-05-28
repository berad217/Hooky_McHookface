"""Tests for the replay engine.

Replay must reuse HandlerManager.dispatch (no divergent path) and support
handler subsetting. These tests use a real in-memory AsyncSQLiteStorage so
the handler_runs persistence is exercised end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hooky.handlers.base import WebhookHandler
from hooky.handlers.manager import HandlerManager
from hooky.replay.engine import ReplayError, replay_webhook
from hooky.storage.models import WebhookRequest
from hooky.storage.sqlite import AsyncSQLiteStorage


class _Recorder(WebhookHandler):
    """Handler that records the webhook ids it processed."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.seen: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def process(self, webhook):  # type: ignore[override]
        self.seen.append(webhook.id)
        return {"handler": self._name, "id": webhook.id}


class _Bomb(WebhookHandler):
    @property
    def name(self) -> str:
        return "bomb"

    async def process(self, webhook):  # type: ignore[override]
        raise RuntimeError("kaboom")


def _make_webhook(path: str = "/webhook/test") -> WebhookRequest:
    return WebhookRequest(
        path=path,
        method="POST",
        headers={"content-type": "application/json"},
        body=b'{"x": 1}',
        body_json={"x": 1},
        received_at=datetime.now(UTC),
        source_ip="127.0.0.1",
    )


async def test_replay_dispatches_to_all_handlers(storage: AsyncSQLiteStorage) -> None:
    """With no subset, every registered handler runs."""
    stored = await storage.store(_make_webhook())
    a, b = _Recorder("a"), _Recorder("b")
    manager = HandlerManager(storage=storage)
    manager.register(a)
    manager.register(b)

    result = await replay_webhook(storage, manager, stored.id)

    assert {r.handler_name for r in result.results} == {"a", "b"}
    assert a.seen == [stored.id]
    assert b.seen == [stored.id]


async def test_replay_records_handler_runs(storage: AsyncSQLiteStorage) -> None:
    """Replay persists handler_runs rows (reuses the storage-backed dispatch)."""
    stored = await storage.store(_make_webhook())
    manager = HandlerManager(storage=storage)
    manager.register(_Recorder("a"))

    await replay_webhook(storage, manager, stored.id)

    runs = await storage.get_handler_runs(stored.id)
    assert len(runs) == 1
    assert runs[0].handler_name == "a"
    assert runs[0].success is True


async def test_replay_subset_only_runs_named_handlers(storage: AsyncSQLiteStorage) -> None:
    """--handler subsetting dispatches only the named handlers."""
    stored = await storage.store(_make_webhook())
    a, b, c = _Recorder("a"), _Recorder("b"), _Recorder("c")
    manager = HandlerManager(storage=storage)
    for h in (a, b, c):
        manager.register(h)

    result = await replay_webhook(storage, manager, stored.id, handler_names=["b"])

    assert [r.handler_name for r in result.results] == ["b"]
    assert a.seen == []
    assert b.seen == [stored.id]
    assert c.seen == []


async def test_replay_unknown_webhook_raises(storage: AsyncSQLiteStorage) -> None:
    manager = HandlerManager(storage=storage)
    with pytest.raises(ReplayError, match="not found"):
        await replay_webhook(storage, manager, "01NONEXISTENTULID0000000000")


async def test_replay_unknown_handler_name_raises(storage: AsyncSQLiteStorage) -> None:
    stored = await storage.store(_make_webhook())
    manager = HandlerManager(storage=storage)
    manager.register(_Recorder("a"))

    with pytest.raises(ReplayError, match="Unknown handler"):
        await replay_webhook(storage, manager, stored.id, handler_names=["nope"])


async def test_replay_isolates_failing_handler(storage: AsyncSQLiteStorage) -> None:
    """A failing handler is recorded as failed; others still succeed (same as live)."""
    stored = await storage.store(_make_webhook())
    manager = HandlerManager(storage=storage)
    manager.register(_Recorder("ok"))
    manager.register(_Bomb())

    result = await replay_webhook(storage, manager, stored.id)

    by_name = {r.handler_name: r for r in result.results}
    assert by_name["ok"].success is True
    assert by_name["bomb"].success is False
    assert "RuntimeError" in (by_name["bomb"].error or "")


async def test_replay_twice_appends_runs(storage: AsyncSQLiteStorage) -> None:
    """Replaying twice produces two sets of handler_runs (no row mutation)."""
    stored = await storage.store(_make_webhook())
    manager = HandlerManager(storage=storage)
    manager.register(_Recorder("a"))

    await replay_webhook(storage, manager, stored.id)
    await replay_webhook(storage, manager, stored.id)

    runs = await storage.get_handler_runs(stored.id)
    assert len(runs) == 2  # two separate runs, both recorded
