"""Tests for the FastAPI routes — happy path, malformed body, store-first.

These tests use async fixtures (in-memory ``AsyncSQLiteStorage``) and the
synchronous FastAPI ``TestClient``. The TestClient runs the app on its own
internal thread/loop via Starlette; storage calls from the test body run on
the test's own event loop. Both end up talking to the same underlying
``aiosqlite`` Connection object, which is thread-safe for our usage pattern
(serialised commits, no concurrent writers).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from hooky.app import create_app
from hooky.storage.base import StorageBackend
from hooky.storage.models import StoredWebhook, WebhookRequest
from hooky.storage.sqlite import AsyncSQLiteStorage


async def test_post_webhook_happy_path_returns_200_and_id(client: TestClient) -> None:
    """A POST with JSON body returns 200 + a webhook id."""
    payload = {"event": "ping", "value": 42}
    resp = client.post("/webhook/test", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert body["id"]
    assert len(body["id"]) == 26  # ULID length


async def test_post_webhook_persists_full_request(
    client: TestClient, storage: AsyncSQLiteStorage
) -> None:
    """The stored webhook captures path, headers, body, parsed body_json."""
    payload = {"event": "ping", "value": 42}
    resp = client.post(
        "/webhook/github",
        json=payload,
        headers={"X-Test-Header": "abc"},
    )
    assert resp.status_code == 200
    webhook_id = resp.json()["id"]

    fetched = await storage.get(webhook_id)
    assert fetched is not None
    assert fetched.path == "/webhook/github"
    assert fetched.method == "POST"
    # Headers keys must be lowercased.
    assert fetched.headers["x-test-header"] == "abc"
    assert "content-type" in fetched.headers
    # httpx serialises JSON compactly (no whitespace between separators).
    assert fetched.body == json.dumps(payload, separators=(",", ":")).encode()
    assert fetched.body_json == payload


async def test_post_webhook_with_malformed_json_stores_bytes_and_null_json(
    client: TestClient, storage: AsyncSQLiteStorage
) -> None:
    """Malformed JSON body still returns 200; body_json is None, body is raw bytes."""
    raw = b"{not valid json"
    resp = client.post(
        "/webhook/junk",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    webhook_id = resp.json()["id"]
    fetched = await storage.get(webhook_id)
    assert fetched is not None
    assert fetched.body == raw
    assert fetched.body_json is None


async def test_post_webhook_with_non_json_content_type_skips_json_parse(
    client: TestClient, storage: AsyncSQLiteStorage
) -> None:
    """Non-JSON Content-Type produces body_json=None even if body parses as JSON."""
    resp = client.post(
        "/webhook/raw",
        content=b'{"this":"is json but content-type isnt"}',
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200

    fetched = await storage.get(resp.json()["id"])
    assert fetched is not None
    assert fetched.body_json is None
    assert b"this" in fetched.body


async def test_post_webhook_with_empty_body_succeeds(
    client: TestClient, storage: AsyncSQLiteStorage
) -> None:
    """An empty POST body still produces a stored row (body=b'', body_json=None)."""
    resp = client.post("/webhook/empty")
    assert resp.status_code == 200

    fetched = await storage.get(resp.json()["id"])
    assert fetched is not None
    assert fetched.body == b""
    assert fetched.body_json is None


async def test_post_webhook_with_nested_subpath(
    client: TestClient, storage: AsyncSQLiteStorage
) -> None:
    """Nested paths like /webhook/foo/bar/baz are captured verbatim."""
    resp = client.post("/webhook/foo/bar/baz", json={"x": 1})
    assert resp.status_code == 200

    fetched = await storage.get(resp.json()["id"])
    assert fetched is not None
    assert fetched.path == "/webhook/foo/bar/baz"


async def test_post_webhook_with_json_array_keeps_body_json_none(
    client: TestClient, storage: AsyncSQLiteStorage
) -> None:
    """JSON arrays don't fit ``body_json: dict | None`` — stored as None, raw body preserved."""
    raw = b"[1, 2, 3]"
    resp = client.post(
        "/webhook/array",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    fetched = await storage.get(resp.json()["id"])
    assert fetched is not None
    assert fetched.body == raw
    assert fetched.body_json is None


async def test_healthz_when_empty_returns_null_last(client: TestClient) -> None:
    """/healthz responds 200 with last_webhook_at=None on an empty store."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["last_webhook_at"] is None


async def test_healthz_reflects_latest_webhook(client: TestClient) -> None:
    """After a POST, /healthz reports a non-null last_webhook_at."""
    client.post("/webhook/test", json={"k": "v"})
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["last_webhook_at"] is not None


# --- Store-first invariant ---------------------------------------------------

class _RaisingStorage(StorageBackend):
    """A storage backend whose store() always raises.

    Used to verify the store-first invariant: if storage fails, the request
    fails with 5xx rather than returning 200 with no record. This is the
    test required by SPEC §7 Sprint 1.
    """

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def store(self, webhook: WebhookRequest) -> StoredWebhook:
        raise RuntimeError("simulated storage failure")

    async def get(self, webhook_id: str) -> StoredWebhook | None:
        return None

    async def list(
        self, limit: int = 50, offset: int = 0, path: str | None = None
    ) -> list[StoredWebhook]:
        return []

    async def get_handler_runs(self, webhook_id: str) -> list[Any]:
        return []

    async def purge_before(self, cutoff: Any) -> int:
        return 0

    async def last_received_at(self) -> Any:
        return None

    async def record_handler_run(self, result: Any) -> None:
        return None

    async def update_signature_status(
        self, webhook_id: str, valid: bool, validator_name: str
    ) -> None:
        return None


def test_store_failure_yields_5xx_no_silent_200() -> None:
    """If storage raises, the route must NOT return 200. Store-first invariant."""
    app = create_app(storage=_RaisingStorage())
    # raise_server_exceptions=False lets TestClient surface the 500 instead of
    # re-raising the underlying exception into the test.
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        resp = failing_client.post("/webhook/test", json={"x": 1})

    assert 500 <= resp.status_code < 600


# --- Sprint 2: dispatch-after-200 -------------------------------------------


async def test_post_webhook_dispatches_handlers_after_200(
    storage: AsyncSQLiteStorage,
) -> None:
    """A POST triggers handler dispatch via BackgroundTasks; handler_runs are recorded.

    BackgroundTasks fire AFTER the response is sent. TestClient blocks for the
    full request cycle (including background tasks) before returning from
    ``client.post``, so by the time we check storage, the runs are persisted.
    """
    from hooky.handlers.base import WebhookHandler
    from hooky.handlers.manager import HandlerManager

    class _Recorder(WebhookHandler):
        def __init__(self) -> None:
            self.seen: list[str] = []

        @property
        def name(self) -> str:
            return "recorder"

        async def process(self, webhook):  # type: ignore[override]
            self.seen.append(webhook.id)
            return {"seen": webhook.id}

    recorder = _Recorder()
    manager = HandlerManager(storage=storage)
    manager.register(recorder)

    app = create_app(storage=storage, handler_manager=manager)
    with TestClient(app) as test_client:
        resp = test_client.post("/webhook/test", json={"hello": "world"})

    assert resp.status_code == 200
    webhook_id = resp.json()["id"]
    assert recorder.seen == [webhook_id]

    # And a handler_runs row exists:
    conn = storage._conn  # type: ignore[attr-defined]
    cursor = await conn.execute(
        "SELECT handler_name, success FROM handler_runs WHERE webhook_id = ?",
        (webhook_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    assert len(rows) == 1
    assert rows[0]["handler_name"] == "recorder"
    assert rows[0]["success"] == 1


async def test_post_webhook_returns_200_even_if_handler_raises(
    storage: AsyncSQLiteStorage,
) -> None:
    """Handler exception must not affect the HTTP response (200 still returned)."""
    from hooky.handlers.base import WebhookHandler
    from hooky.handlers.manager import HandlerManager

    class _Bomb(WebhookHandler):
        @property
        def name(self) -> str:
            return "bomb"

        async def process(self, webhook):  # type: ignore[override]
            raise RuntimeError("nope")

    manager = HandlerManager(storage=storage)
    manager.register(_Bomb())

    app = create_app(storage=storage, handler_manager=manager)
    with TestClient(app) as test_client:
        resp = test_client.post("/webhook/test", json={"x": 1})

    assert resp.status_code == 200
    webhook_id = resp.json()["id"]
    conn = storage._conn  # type: ignore[attr-defined]
    cursor = await conn.execute(
        "SELECT handler_name, success, error FROM handler_runs WHERE webhook_id = ?",
        (webhook_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    assert len(rows) == 1
    assert rows[0]["success"] == 0
    assert "RuntimeError" in rows[0]["error"]


# --- Sprint 3: signature validation -----------------------------------------


import hashlib  # noqa: E402 — grouped with Sprint 3 imports
import hmac  # noqa: E402

from hooky.handlers.base import WebhookHandler  # noqa: E402
from hooky.handlers.manager import HandlerManager  # noqa: E402
from hooky.validators.github import GitHubValidator  # noqa: E402


def _gh_sig256(secret: str, body: bytes) -> str:
    """Helper: produce a GitHub-format SHA256 signature."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _Recorder(WebhookHandler):
    """Test handler that records every webhook id it sees."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def name(self) -> str:
        return "recorder"

    async def process(self, webhook):  # type: ignore[override]
        self.seen.append(webhook.id)
        return {"seen": webhook.id}


async def test_valid_signature_returns_200_and_dispatches(
    storage: AsyncSQLiteStorage,
) -> None:
    """A correctly-signed POST returns 200, stores with signature_valid=1, dispatches handlers."""
    secret = "shh"
    recorder = _Recorder()
    manager = HandlerManager(storage=storage)
    manager.register(recorder)

    validators = {"/webhook/gh": GitHubValidator(secret=secret)}
    app = create_app(storage=storage, handler_manager=manager, validators=validators)

    body = b'{"event":"ping"}'
    sig = _gh_sig256(secret, body)
    with TestClient(app) as test_client:
        resp = test_client.post(
            "/webhook/gh",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )

    assert resp.status_code == 200
    webhook_id = resp.json()["id"]

    fetched = await storage.get(webhook_id)
    assert fetched is not None
    assert fetched.signature_valid is True
    assert fetched.signature_validator == "GitHubValidator"
    assert recorder.seen == [webhook_id]


async def test_invalid_signature_returns_401_stored_no_dispatch(
    storage: AsyncSQLiteStorage,
) -> None:
    """A wrongly-signed POST returns 401, is stored with signature_valid=0, NO handler runs."""
    recorder = _Recorder()
    manager = HandlerManager(storage=storage)
    manager.register(recorder)

    validators = {"/webhook/gh": GitHubValidator(secret="real-secret")}
    app = create_app(storage=storage, handler_manager=manager, validators=validators)

    body = b'{"event":"ping"}'
    # Sign with the wrong secret -> mismatch
    bad_sig = _gh_sig256("wrong-secret", body)
    with TestClient(app) as test_client:
        resp = test_client.post(
            "/webhook/gh",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": bad_sig},
        )

    assert resp.status_code == 401
    body_resp = resp.json()
    assert body_resp["status"] == "rejected"
    assert body_resp["reason"] == "invalid_signature"
    webhook_id = body_resp["id"]

    # Store-first invariant: row exists, signature_valid=False
    fetched = await storage.get(webhook_id)
    assert fetched is not None
    assert fetched.signature_valid is False
    assert fetched.signature_validator == "GitHubValidator"

    # No handler_runs row for the rejected webhook
    assert recorder.seen == []
    conn = storage._conn  # type: ignore[attr-defined]
    cursor = await conn.execute(
        "SELECT COUNT(*) AS c FROM handler_runs WHERE webhook_id = ?", (webhook_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row["c"] == 0


async def test_path_without_validator_passes_through_unchanged(
    storage: AsyncSQLiteStorage,
) -> None:
    """Posts to a path with no configured validator behave as in Sprint 2."""
    recorder = _Recorder()
    manager = HandlerManager(storage=storage)
    manager.register(recorder)

    # Validator on /webhook/gh; we'll post to /webhook/other.
    validators = {"/webhook/gh": GitHubValidator(secret="s")}
    app = create_app(storage=storage, handler_manager=manager, validators=validators)

    with TestClient(app) as test_client:
        resp = test_client.post("/webhook/other", json={"x": 1})

    assert resp.status_code == 200
    webhook_id = resp.json()["id"]
    fetched = await storage.get(webhook_id)
    assert fetched is not None
    # No validator ran -> stays NULL
    assert fetched.signature_valid is None
    assert fetched.signature_validator is None
    # Handler still dispatched
    assert recorder.seen == [webhook_id]


async def test_missing_signature_header_returns_401(
    storage: AsyncSQLiteStorage,
) -> None:
    """A POST to a guarded path with no signature header is rejected (401, stored)."""
    validators = {"/webhook/gh": GitHubValidator(secret="s")}
    app = create_app(storage=storage, validators=validators)

    with TestClient(app) as test_client:
        resp = test_client.post("/webhook/gh", json={"x": 1})

    assert resp.status_code == 401
    webhook_id = resp.json()["id"]
    fetched = await storage.get(webhook_id)
    assert fetched is not None
    assert fetched.signature_valid is False
