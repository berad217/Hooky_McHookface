"""Tests for :class:`HandlerManager` — dispatch, isolation, persistence.

These tests are the heart of Sprint 2's contract. They verify:

- duplicate registration is rejected,
- ``dispatch`` runs all handlers concurrently,
- one handler raising never affects another,
- a row is recorded in ``handler_runs`` per handler (success or failure),
- empty manager returns an empty list (no crash),
- order of results matches registration order (so replay can correlate).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hooky.handlers.base import WebhookHandler
from hooky.handlers.manager import HandlerManager
from hooky.storage.sqlite import AsyncSQLiteStorage


class _RecordingHandler(WebhookHandler):
    """A handler that records call order and optionally sleeps + returns a value."""

    def __init__(self, name: str, *, sleep_s: float = 0.0, return_value: Any = None) -> None:
        self._name = name
        self._sleep_s = sleep_s
        self._return = return_value
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def process(self, webhook):  # type: ignore[override]
        self.calls.append(webhook.id)
        if self._sleep_s:
            await asyncio.sleep(self._sleep_s)
        return self._return


class _BoomHandler(WebhookHandler):
    """A handler whose ``process`` always raises."""

    def __init__(self, name: str = "boom") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def process(self, webhook):  # type: ignore[override]
        raise RuntimeError(f"handler {self._name} exploded")


# -- registration -----------------------------------------------------------


def test_register_rejects_duplicate_name() -> None:
    """Two handlers with the same name cannot coexist on one manager."""
    mgr = HandlerManager(storage=None)
    mgr.register(_RecordingHandler("dup"))
    with pytest.raises(ValueError):
        mgr.register(_RecordingHandler("dup"))


def test_register_rejects_empty_name() -> None:
    """A handler whose ``name`` is empty fails fast."""

    class Anon(WebhookHandler):
        @property
        def name(self) -> str:
            return ""

        async def process(self, webhook):  # type: ignore[override]
            return None

    mgr = HandlerManager(storage=None)
    with pytest.raises(ValueError):
        mgr.register(Anon())


def test_handlers_snapshot_is_a_copy() -> None:
    """``manager.handlers`` returns a fresh list so callers can't mutate state."""
    mgr = HandlerManager(storage=None)
    h = _RecordingHandler("a")
    mgr.register(h)
    snapshot = mgr.handlers
    snapshot.clear()
    assert len(mgr) == 1


# -- dispatch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_with_no_handlers_returns_empty(make_stored) -> None:
    """Empty manager dispatches cleanly and returns an empty list."""
    mgr = HandlerManager(storage=None)
    results = await mgr.dispatch(make_stored())
    assert results == []


@pytest.mark.asyncio
async def test_dispatch_runs_all_handlers(make_stored) -> None:
    """Every registered handler gets called exactly once per dispatch."""
    mgr = HandlerManager(storage=None)
    a, b, c = _RecordingHandler("a"), _RecordingHandler("b"), _RecordingHandler("c")
    for h in (a, b, c):
        mgr.register(h)

    webhook = make_stored()
    results = await mgr.dispatch(webhook)

    assert len(results) == 3
    assert {r.handler_name for r in results} == {"a", "b", "c"}
    assert all(r.success for r in results)
    assert a.calls == [webhook.id]
    assert b.calls == [webhook.id]
    assert c.calls == [webhook.id]


@pytest.mark.asyncio
async def test_dispatch_results_preserve_registration_order(make_stored) -> None:
    """Result order matches registration order (replay relies on this)."""
    mgr = HandlerManager(storage=None)
    names = ["first", "second", "third", "fourth"]
    for n in names:
        mgr.register(_RecordingHandler(n))
    results = await mgr.dispatch(make_stored())
    assert [r.handler_name for r in results] == names


@pytest.mark.asyncio
async def test_dispatch_isolates_handler_exception(make_stored) -> None:
    """One handler raising never prevents others from running or completing."""
    mgr = HandlerManager(storage=None)
    good_a = _RecordingHandler("good_a", return_value={"ok": True})
    boom = _BoomHandler("boom")
    good_b = _RecordingHandler("good_b", return_value=42)
    mgr.register(good_a)
    mgr.register(boom)
    mgr.register(good_b)

    webhook = make_stored()
    results = await mgr.dispatch(webhook)

    by_name = {r.handler_name: r for r in results}
    assert by_name["good_a"].success is True
    assert by_name["good_b"].success is True
    assert by_name["boom"].success is False
    assert "RuntimeError" in (by_name["boom"].error or "")
    assert by_name["boom"].traceback is not None
    # The successful handlers actually ran:
    assert good_a.calls == [webhook.id]
    assert good_b.calls == [webhook.id]


@pytest.mark.asyncio
async def test_dispatch_handles_all_handlers_failing(make_stored) -> None:
    """A dispatch where every handler raises still returns one result per."""
    mgr = HandlerManager(storage=None)
    mgr.register(_BoomHandler("boom1"))
    mgr.register(_BoomHandler("boom2"))
    results = await mgr.dispatch(make_stored())
    assert len(results) == 2
    assert all(not r.success for r in results)
    assert all(r.error and "RuntimeError" in r.error for r in results)
    assert all(r.traceback for r in results)


@pytest.mark.asyncio
async def test_dispatch_runs_handlers_concurrently(make_stored) -> None:
    """Two 50ms-sleeping handlers complete in well under 100ms (concurrent, not serial)."""
    mgr = HandlerManager(storage=None)
    mgr.register(_RecordingHandler("slow_a", sleep_s=0.05))
    mgr.register(_RecordingHandler("slow_b", sleep_s=0.05))

    loop = asyncio.get_event_loop()
    start = loop.time()
    results = await mgr.dispatch(make_stored())
    elapsed = loop.time() - start

    assert len(results) == 2
    assert all(r.success for r in results)
    # Serial would be ~0.10s; concurrent should be ~0.05s. Allow generous slack.
    assert elapsed < 0.09, f"dispatch ran serially? elapsed={elapsed:.3f}s"


# -- persistence ------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_persists_handler_runs(make_stored, storage: AsyncSQLiteStorage) -> None:
    """Each result becomes a row in ``handler_runs`` when storage is wired."""
    # The stored webhook must really exist in the DB (FK on handler_runs.webhook_id).
    stored = await storage.store(make_stored().model_copy(update={"id": "ignored_by_store"}))
    mgr = HandlerManager(storage=storage)
    mgr.register(_RecordingHandler("ok", return_value={"value": 1}))
    mgr.register(_BoomHandler("bad"))

    results = await mgr.dispatch(stored)
    assert len(results) == 2

    conn = storage._conn  # type: ignore[attr-defined]
    cursor = await conn.execute(
        "SELECT handler_name, success, error, return_value FROM handler_runs "
        "WHERE webhook_id = ? ORDER BY id",
        (stored.id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    by_name = {row["handler_name"]: row for row in rows}
    assert by_name["ok"]["success"] == 1
    assert by_name["ok"]["return_value"] == '{"value": 1}'
    assert by_name["bad"]["success"] == 0
    assert "RuntimeError" in by_name["bad"]["error"]


@pytest.mark.asyncio
async def test_dispatch_without_storage_does_not_persist(make_stored) -> None:
    """When storage is ``None``, dispatch still returns results without crashing."""
    mgr = HandlerManager(storage=None)
    mgr.register(_RecordingHandler("a", return_value=1))
    results = await mgr.dispatch(make_stored())
    assert len(results) == 1
    assert results[0].success is True
    assert results[0].return_value == 1


@pytest.mark.asyncio
async def test_dispatch_swallows_persist_failure(make_stored) -> None:
    """A storage failure during ``record_handler_run`` must not propagate."""

    class _FailingPersist(AsyncSQLiteStorage):
        async def record_handler_run(self, result):  # type: ignore[override]
            raise RuntimeError("disk full")

    backend = _FailingPersist(":memory:")
    await backend.connect()
    try:
        mgr = HandlerManager(storage=backend)
        mgr.register(_RecordingHandler("a", return_value=1))
        # Must NOT raise even though persistence fails.
        results = await mgr.dispatch(make_stored())
        assert len(results) == 1
        assert results[0].success is True
    finally:
        await backend.close()
