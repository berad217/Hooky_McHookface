"""App-level enforcement of ``storage.retention_days``.

`retention_days` shipped in Sprint 1 as a declared-but-dead field ("Enforcement
not in Sprint 1"), which left the store-first invariant without a counterweight:
every webhook body is persisted BEFORE the validator and BEFORE handler
dispatch, so a request that is ultimately rejected -- a bad signature, or a
sender a handler denies -- was kept in full, forever, and travelled into any
backup that replicates the database off-machine.

These tests pin the enforcement contract at the app boundary. The storage half
(`purge_before`) is covered in ``test_storage/test_sqlite.py``.

Note these build the app WITHOUT injecting storage: an injected backend is
owned by the caller, so the lifespan never connects it and the purge would find
no connection. Letting the app own its backend is what exercises the real path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from hooky.app import create_app
from hooky.config import load_config
from hooky.storage.models import WebhookRequest
from hooky.storage.sqlite import AsyncSQLiteStorage


async def _seed(db: str, age_days: int) -> str:
    """Put one webhook of the given age on disk; return its id."""
    backend = AsyncSQLiteStorage(db)
    await backend.connect()
    stored = await backend.store(
        WebhookRequest(
            path="/webhook/telegram",
            method="POST",
            headers={"content-type": "application/json"},
            body=b'{"message": {"text": "from a sender who was denied"}}',
            body_json={"message": {"text": "from a sender who was denied"}},
            received_at=datetime.now(UTC) - timedelta(days=age_days),
            source_ip="1.2.3.4",
        )
    )
    await backend.close()
    return stored.id


async def _exists(db: str, webhook_id: str) -> bool:
    backend = AsyncSQLiteStorage(db)
    await backend.connect()
    try:
        return await backend.get(webhook_id) is not None
    finally:
        await backend.close()


def _config(db: str, retention_days: int):
    cfg = load_config()
    cfg.storage.path = db
    cfg.storage.retention_days = retention_days
    return cfg


async def test_app_purges_expired_webhooks_on_startup(tmp_path) -> None:
    """Retention has to apply to what is ALREADY on disk. The store this was
    written for held 101 rows and had never expired one."""
    db = str(tmp_path / "h.db")
    old = await _seed(db, age_days=99)

    with TestClient(create_app(config=_config(db, 30))):
        pass

    assert not await _exists(db, old)


async def test_recent_webhooks_survive_startup(tmp_path) -> None:
    db = str(tmp_path / "h.db")
    recent = await _seed(db, age_days=2)

    with TestClient(create_app(config=_config(db, 30))):
        pass

    assert await _exists(db, recent)


async def test_retention_zero_keeps_forever(tmp_path) -> None:
    """0 is the documented opt-out and must be honoured -- a purge that ran
    anyway would destroy data the operator explicitly asked to keep."""
    db = str(tmp_path / "h.db")
    ancient = await _seed(db, age_days=9999)

    with TestClient(create_app(config=_config(db, 0))):
        pass

    assert await _exists(db, ancient)


async def test_a_failing_purge_does_not_take_the_app_down(tmp_path, monkeypatch) -> None:
    """Retention is hygiene, not a health gate. A webhook receiver that refuses
    to boot because it could not delete old rows is worse than one still holding
    them -- it drops live traffic to fix a privacy nicety."""
    db = str(tmp_path / "h.db")

    async def boom(self, cutoff):  # noqa: ANN001
        raise RuntimeError("simulated purge failure")

    monkeypatch.setattr(AsyncSQLiteStorage, "purge_before", boom)

    with TestClient(create_app(config=_config(db, 30))) as client:
        assert client.get("/healthz").status_code == 200
