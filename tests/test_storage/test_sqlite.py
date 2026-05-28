"""Tests for AsyncSQLiteStorage — schema, round-trip, listing, health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hooky.storage.models import HandlerResult, WebhookRequest
from hooky.storage.sqlite import AsyncSQLiteStorage

_UNSET: object = object()


def _make_webhook(
    path: str = "/webhook/test",
    body: bytes = b'{"hello": "world"}',
    body_json: dict | None | object = _UNSET,
    received_at: datetime | None = None,
) -> WebhookRequest:
    """Construct a WebhookRequest with sensible defaults for tests.

    ``body_json`` uses a sentinel so callers can explicitly pass ``None`` to
    indicate "no JSON parse" without it being mistaken for "default please".
    """
    if body_json is _UNSET:
        body_json = {"hello": "world"}
    return WebhookRequest(
        path=path,
        method="POST",
        headers={"content-type": "application/json", "x-test": "1"},
        body=body,
        body_json=body_json,  # type: ignore[arg-type]
        received_at=received_at or datetime.now(UTC),
        source_ip="127.0.0.1",
    )


async def test_store_roundtrip_preserves_all_fields(storage: AsyncSQLiteStorage) -> None:
    """A stored webhook can be retrieved with every field intact."""
    webhook = _make_webhook()
    stored = await storage.store(webhook)

    assert stored.id  # ULID assigned
    assert len(stored.id) == 26  # ULIDs are 26 chars in canonical form

    fetched = await storage.get(stored.id)
    assert fetched is not None
    assert fetched.id == stored.id
    assert fetched.path == webhook.path
    assert fetched.method == webhook.method
    assert fetched.headers == webhook.headers
    assert fetched.body == webhook.body
    assert fetched.body_json == webhook.body_json
    assert fetched.source_ip == webhook.source_ip
    # received_at may lose sub-second precision in ISO round-trip; allow small delta
    assert abs((fetched.received_at - webhook.received_at).total_seconds()) < 0.001


async def test_store_assigns_unique_ulids(storage: AsyncSQLiteStorage) -> None:
    """Two stores yield two distinct, time-sortable ULIDs."""
    a = await storage.store(_make_webhook())
    b = await storage.store(_make_webhook())
    assert a.id != b.id
    # ULIDs are lexicographically sortable by time; b was stored after a.
    assert b.id > a.id


async def test_body_stored_as_bytes_even_when_not_json(storage: AsyncSQLiteStorage) -> None:
    """Non-JSON bodies persist as raw bytes; body_json stays None."""
    webhook = _make_webhook(body=b"\x00\x01\x02 not json", body_json=None)
    stored = await storage.store(webhook)
    fetched = await storage.get(stored.id)
    assert fetched is not None
    assert fetched.body == b"\x00\x01\x02 not json"
    assert fetched.body_json is None


async def test_get_returns_none_for_unknown_id(storage: AsyncSQLiteStorage) -> None:
    """Looking up a nonexistent id returns None, not an exception."""
    result = await storage.get("01HZZZZZZZZZZZZZZZZZZZZZZZ")
    assert result is None


async def test_list_orders_most_recent_first(storage: AsyncSQLiteStorage) -> None:
    """list() returns webhooks sorted by received_at desc."""
    now = datetime.now(UTC)
    older = await storage.store(_make_webhook(received_at=now - timedelta(minutes=5)))
    newer = await storage.store(_make_webhook(received_at=now))

    rows = await storage.list()
    assert len(rows) == 2
    assert rows[0].id == newer.id
    assert rows[1].id == older.id


async def test_list_respects_limit_and_offset(storage: AsyncSQLiteStorage) -> None:
    """Pagination via limit + offset returns expected slices."""
    base = datetime.now(UTC)
    ids = []
    for i in range(5):
        stored = await storage.store(
            _make_webhook(received_at=base - timedelta(seconds=i))
        )
        ids.append(stored.id)

    page = await storage.list(limit=2, offset=1)
    assert len(page) == 2
    # Most recent is index 0 in our insert loop (smallest timedelta).
    assert page[0].id == ids[1]
    assert page[1].id == ids[2]


async def test_last_received_at_empty_returns_none(storage: AsyncSQLiteStorage) -> None:
    """When no webhooks exist, last_received_at returns None."""
    assert await storage.last_received_at() is None


async def test_last_received_at_returns_most_recent(storage: AsyncSQLiteStorage) -> None:
    """last_received_at reflects the most recently stored webhook's timestamp."""
    now = datetime.now(UTC)
    await storage.store(_make_webhook(received_at=now - timedelta(minutes=1)))
    await storage.store(_make_webhook(received_at=now))

    latest = await storage.last_received_at()
    assert latest is not None
    assert abs((latest - now).total_seconds()) < 0.001


async def test_connect_is_idempotent(storage: AsyncSQLiteStorage) -> None:
    """Calling connect() on an already-connected store is a no-op."""
    # The fixture already called connect once. A second call must not raise.
    await storage.connect()
    # And the store must still be usable.
    stored = await storage.store(_make_webhook())
    assert stored.id


async def test_close_is_idempotent() -> None:
    """Closing twice does not raise."""
    backend = AsyncSQLiteStorage(":memory:")
    await backend.connect()
    await backend.close()
    await backend.close()  # second close: must not raise


async def test_store_requires_connection() -> None:
    """Operations before connect() raise a clear RuntimeError."""
    backend = AsyncSQLiteStorage(":memory:")
    with pytest.raises(RuntimeError, match="not connected"):
        await backend.store(_make_webhook())


async def test_record_handler_run_persists_row(storage: AsyncSQLiteStorage) -> None:
    """record_handler_run inserts into handler_runs (Sprint 2 will exercise this)."""
    stored = await storage.store(_make_webhook())
    now = datetime.now(UTC)
    result = HandlerResult(
        handler_name="echo",
        webhook_id=stored.id,
        started_at=now,
        completed_at=now + timedelta(milliseconds=5),
        success=True,
        return_value={"ok": True},
    )
    await storage.record_handler_run(result)

    # Verify via raw query — handler_runs has no public read API in Sprint 1.
    conn = storage._require_conn()  # type: ignore[attr-defined]
    cursor = await conn.execute(
        "SELECT handler_name, success FROM handler_runs WHERE webhook_id = ?",
        (stored.id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row["handler_name"] == "echo"
    assert row["success"] == 1


async def test_update_signature_status_sets_valid_true(storage: AsyncSQLiteStorage) -> None:
    """update_signature_status flips signature_valid + signature_validator on the row."""
    stored = await storage.store(_make_webhook())
    # Sprint 1 invariant: row starts with signature_valid=None.
    assert stored.signature_valid is None
    assert stored.signature_validator is None

    await storage.update_signature_status(stored.id, True, "GitHubValidator")

    fetched = await storage.get(stored.id)
    assert fetched is not None
    assert fetched.signature_valid is True
    assert fetched.signature_validator == "GitHubValidator"


async def test_update_signature_status_sets_valid_false(storage: AsyncSQLiteStorage) -> None:
    """A False outcome persists as signature_valid=False (still stored = debuggable)."""
    stored = await storage.store(_make_webhook())
    await storage.update_signature_status(stored.id, False, "StripeValidator")

    fetched = await storage.get(stored.id)
    assert fetched is not None
    assert fetched.signature_valid is False
    assert fetched.signature_validator == "StripeValidator"


async def test_file_backed_storage_persists_across_reconnect(tmp_path: Path) -> None:
    """A file-backed store keeps rows across a close/reopen cycle."""
    db_path = tmp_path / "subdir" / "hooky.db"  # exercises mkdir
    backend = AsyncSQLiteStorage(db_path)
    await backend.connect()
    stored = await backend.store(_make_webhook())
    await backend.close()

    backend2 = AsyncSQLiteStorage(db_path)
    await backend2.connect()
    fetched = await backend2.get(stored.id)
    await backend2.close()

    assert fetched is not None
    assert fetched.id == stored.id
