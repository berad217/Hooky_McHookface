"""Pydantic data models for webhook storage.

Models per SPEC §6. ``HandlerResult`` is defined here for forward-compatibility
with Sprint 2 (handler_runs table), even though no handlers run in Sprint 1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WebhookRequest(BaseModel):
    """A webhook as received, prior to assignment of a storage id.

    Attributes:
        path: Full webhook path (e.g. ``"/webhook/github"``).
        method: HTTP method. ``"POST"`` in v1.
        headers: Request headers, lowercased keys.
        body: Raw request body as bytes.
        body_json: Parsed JSON dict if Content-Type was JSON-shaped *and*
            parsing succeeded; otherwise ``None``.
        received_at: UTC timestamp of receipt.
        source_ip: Best-effort client IP, ``None`` if unavailable.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: str
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""
    body_json: dict[str, Any] | None = None
    received_at: datetime
    source_ip: str | None = None


class StoredWebhook(WebhookRequest):
    """A webhook that has been persisted and assigned an id.

    Attributes:
        id: ULID assigned at store time. Sortable by time.
        signature_valid: ``None`` if no validator configured, ``True`` /
            ``False`` once a validator has run. Always ``None`` in Sprint 1.
        signature_validator: Class name of the validator that ran (or ``None``).
    """

    id: str
    signature_valid: bool | None = None
    signature_validator: str | None = None


class HandlerResult(BaseModel):
    """The outcome of a single handler run against a stored webhook.

    Sprint 1 defines the model but does not exercise it. Sprint 2 produces
    these from ``HandlerManager.dispatch`` and persists them via
    :meth:`StorageBackend.record_handler_run`.
    """

    handler_name: str
    webhook_id: str
    started_at: datetime
    completed_at: datetime
    success: bool
    return_value: Any | None = None
    error: str | None = None
    traceback: str | None = None
