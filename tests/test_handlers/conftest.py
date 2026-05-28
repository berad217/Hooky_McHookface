"""Shared fixtures + helpers for handler tests.

Provides a ``make_stored`` factory for constructing :class:`StoredWebhook`
instances without going through the route layer — keeps handler tests fast
and independent of the FastAPI machinery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from hooky.storage.models import StoredWebhook


@pytest.fixture
def make_stored():
    """Factory fixture: build a :class:`StoredWebhook` with sensible defaults."""

    def _make(
        webhook_id: str = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        path: str = "/webhook/test",
        body: bytes = b'{"k":"v"}',
        body_json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> StoredWebhook:
        return StoredWebhook(
            id=webhook_id,
            path=path,
            method="POST",
            headers=headers if headers is not None else {"content-type": "application/json"},
            body=body,
            body_json=body_json if body_json is not None else {"k": "v"},
            received_at=datetime.now(UTC),
            source_ip="127.0.0.1",
            signature_valid=None,
            signature_validator=None,
        )

    return _make
