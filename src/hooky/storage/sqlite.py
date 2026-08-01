"""Async SQLite storage backend.

Schema follows SPEC §6 exactly. ULIDs are used as primary keys for natural
time-sortability without requiring an extra index on insertion order.

aiosqlite is used for non-blocking I/O. A single connection is held for the
lifetime of the backend; aiosqlite serialises access internally, which is
appropriate for this workload (low write rate, append-mostly).
"""

from __future__ import annotations

import builtins
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from ulid import ULID

from hooky.storage.base import StorageBackend
from hooky.storage.models import HandlerResult, StoredWebhook, WebhookRequest

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  method TEXT NOT NULL,
  headers TEXT NOT NULL,
  body BLOB NOT NULL,
  body_json TEXT,
  received_at TEXT NOT NULL,
  source_ip TEXT,
  signature_valid INTEGER,
  signature_validator TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhooks_path_received
  ON webhooks(path, received_at DESC);

CREATE TABLE IF NOT EXISTS handler_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  webhook_id TEXT NOT NULL REFERENCES webhooks(id),
  handler_name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  success INTEGER NOT NULL,
  return_value TEXT,
  error TEXT,
  traceback TEXT
);

CREATE INDEX IF NOT EXISTS idx_handler_runs_webhook
  ON handler_runs(webhook_id);
"""


def _sig_to_int(value: bool | None) -> int | None:
    """Translate ``signature_valid`` to its SQLite storage form."""
    if value is None:
        return None
    return 1 if value else 0


def _sig_from_int(value: int | None) -> bool | None:
    """Translate ``signature_valid`` from SQLite storage form."""
    if value is None:
        return None
    return bool(value)


class AsyncSQLiteStorage(StorageBackend):
    """Async SQLite-backed implementation of :class:`StorageBackend`."""

    def __init__(self, path: str | Path = "data/hooky.db") -> None:
        """Initialise the backend.

        Args:
            path: Filesystem path to the SQLite database. Use ``":memory:"``
                for an ephemeral in-memory database (useful for tests).
        """
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the connection and apply the schema. Idempotent."""
        if self._conn is not None:
            return

        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        """Return the live connection or raise."""
        if self._conn is None:
            raise RuntimeError(
                "AsyncSQLiteStorage is not connected. Call await storage.connect() first."
            )
        return self._conn

    async def store(self, webhook: WebhookRequest) -> StoredWebhook:
        """Persist a webhook with a fresh ULID and return the stored row."""
        conn = self._require_conn()
        webhook_id = str(ULID())

        stored = StoredWebhook(
            id=webhook_id,
            path=webhook.path,
            method=webhook.method,
            headers=webhook.headers,
            body=webhook.body,
            body_json=webhook.body_json,
            received_at=webhook.received_at,
            source_ip=webhook.source_ip,
            signature_valid=None,
            signature_validator=None,
        )

        await conn.execute(
            """
            INSERT INTO webhooks (
                id, path, method, headers, body, body_json,
                received_at, source_ip, signature_valid, signature_validator
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored.id,
                stored.path,
                stored.method,
                json.dumps(stored.headers),
                stored.body,
                json.dumps(stored.body_json) if stored.body_json is not None else None,
                stored.received_at.isoformat(),
                stored.source_ip,
                _sig_to_int(stored.signature_valid),
                stored.signature_validator,
            ),
        )
        await conn.commit()
        return stored

    async def purge_before(self, cutoff: datetime) -> int:
        """Delete webhooks received before ``cutoff`` (and their handler runs).

        Handler runs go first: ``handler_runs.webhook_id`` references
        ``webhooks(id)``, so deleting the parent first would leave dangling rows
        whose ``return_value`` can echo request-derived data — the exact content
        retention is meant to expire.

        Both statements run inside one transaction and commit together, so a
        crash mid-purge cannot leave handler runs orphaned from their webhooks.
        """
        conn = self._require_conn()
        iso = cutoff.isoformat()
        await conn.execute(
            """
            DELETE FROM handler_runs
             WHERE webhook_id IN (SELECT id FROM webhooks WHERE received_at < ?)
            """,
            (iso,),
        )
        cursor = await conn.execute(
            "DELETE FROM webhooks WHERE received_at < ?", (iso,)
        )
        deleted = cursor.rowcount or 0
        await conn.commit()
        return deleted

    async def get(self, webhook_id: str) -> StoredWebhook | None:
        """Look up a stored webhook by id."""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT * FROM webhooks WHERE id = ?",
            (webhook_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return self._row_to_stored(row)

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        path: str | None = None,
    ) -> list[StoredWebhook]:
        """List stored webhooks, most recent first, optionally filtered by path."""
        conn = self._require_conn()
        if path is None:
            cursor = await conn.execute(
                """
                SELECT * FROM webhooks
                ORDER BY received_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT * FROM webhooks
                WHERE path = ?
                ORDER BY received_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (path, limit, offset),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_stored(row) for row in rows]

    # builtins.list: the `list` method shadows the `list` builtin in this
    # class's annotation scope under `from __future__ import annotations`.
    async def get_handler_runs(self, webhook_id: str) -> builtins.list[HandlerResult]:
        """Return handler runs for a webhook, oldest first."""
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT webhook_id, handler_name, started_at, completed_at,
                   success, return_value, error, traceback
            FROM handler_runs
            WHERE webhook_id = ?
            ORDER BY started_at ASC, id ASC
            """,
            (webhook_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_handler_result(row) for row in rows]

    async def last_received_at(self) -> datetime | None:
        """Return the most recent ``received_at`` (or ``None`` if empty)."""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT received_at FROM webhooks ORDER BY received_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return datetime.fromisoformat(row["received_at"])

    async def update_signature_status(
        self,
        webhook_id: str,
        valid: bool,
        validator_name: str,
    ) -> None:
        """Update signature_valid + signature_validator on an existing row.

        Sprint 3 calls this from the route after ``store()`` completes and
        the validator has run. We deliberately do *not* check ``rowcount``
        and raise on zero: the row was just written by us in the same
        request; if it's gone, something has corrupted the DB and the next
        query will surface that loudly anyway.
        """
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE webhooks
            SET signature_valid = ?, signature_validator = ?
            WHERE id = ?
            """,
            (_sig_to_int(valid), validator_name, webhook_id),
        )
        await conn.commit()

    async def record_handler_run(self, result: HandlerResult) -> None:
        """Persist a handler run. Not exercised in Sprint 1."""
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO handler_runs (
                webhook_id, handler_name, started_at, completed_at,
                success, return_value, error, traceback
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.webhook_id,
                result.handler_name,
                result.started_at.isoformat(),
                result.completed_at.isoformat(),
                1 if result.success else 0,
                json.dumps(result.return_value) if result.return_value is not None else None,
                result.error,
                result.traceback,
            ),
        )
        await conn.commit()

    @staticmethod
    def _row_to_handler_result(row: aiosqlite.Row) -> HandlerResult:
        """Convert a handler_runs row to a :class:`HandlerResult`."""
        return_value_raw: str | None = row["return_value"]
        return_value: Any | None = (
            json.loads(return_value_raw) if return_value_raw is not None else None
        )
        return HandlerResult(
            handler_name=row["handler_name"],
            webhook_id=row["webhook_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]),
            success=bool(row["success"]),
            return_value=return_value,
            error=row["error"],
            traceback=row["traceback"],
        )

    @staticmethod
    def _row_to_stored(row: aiosqlite.Row) -> StoredWebhook:
        """Convert a DB row to a :class:`StoredWebhook`."""
        body_json_raw: str | None = row["body_json"]
        body_json: dict[str, Any] | None = (
            json.loads(body_json_raw) if body_json_raw is not None else None
        )

        return StoredWebhook(
            id=row["id"],
            path=row["path"],
            method=row["method"],
            headers=json.loads(row["headers"]),
            body=bytes(row["body"]),
            body_json=body_json,
            received_at=datetime.fromisoformat(row["received_at"]),
            source_ip=row["source_ip"],
            signature_valid=_sig_from_int(row["signature_valid"]),
            signature_validator=row["signature_validator"],
        )
