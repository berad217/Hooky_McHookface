"""FastAPI routes for the webhook receiver.

Sprint 1 shipped two endpoints:

- ``POST /webhook/{path:path}`` — receive, store, return 200.
- ``GET /healthz`` — liveness probe + last-webhook timestamp.

Sprint 2 added handler dispatch after the 200 via ``BackgroundTasks``.

Sprint 3 adds signature validation:

1. Body is read, headers normalised, request stored (signature_valid=NULL).
   Store-first invariant holds — every received webhook is durably persisted
   before anything else runs.
2. If a validator is configured for the exact request path, it runs against
   the raw body + headers. The webhook row's ``signature_valid`` and
   ``signature_validator`` columns are updated with the outcome.
3. On invalid signature: route returns **401** and skips handler dispatch.
   The webhook stays in storage for debugging / manual replay.
4. On valid signature (or no validator configured): handlers dispatch via
   :class:`BackgroundTasks` and the route returns 200, exactly as before.

Validator exceptions are NOT caught here — a crashing validator is a
programmer bug, not a network attacker, and surfacing the 5xx makes it
debuggable. (The webhook is still durably stored thanks to step 1.)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Request, Response
from fastapi.responses import JSONResponse

from hooky.handlers.manager import HandlerManager
from hooky.log import get_logger
from hooky.storage.base import StorageBackend
from hooky.storage.models import StoredWebhook, WebhookRequest
from hooky.tunnel.base import TunnelProvider
from hooky.validators.base import SignatureValidator

logger = get_logger(__name__)


def _normalise_headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """Lowercase header keys and decode to str.

    Args:
        raw: Raw header pairs from the ASGI scope.

    Returns:
        Dict with lowercase keys and string values.
    """
    out: dict[str, str] = {}
    for key, value in raw:
        out[key.decode("latin-1").lower()] = value.decode("latin-1")
    return out


def _parse_body_json(body: bytes, content_type: str | None) -> dict[str, object] | None:
    """Attempt to parse ``body`` as JSON if Content-Type looks JSON-shaped.

    A best-effort parse — failures return ``None`` rather than raising so the
    raw bytes are still stored.

    Args:
        body: Raw request body.
        content_type: Value of the Content-Type header (may be ``None``).

    Returns:
        Parsed dict if successful and the parsed value is a dict, else ``None``.
        We deliberately do not store JSON arrays / scalars in ``body_json`` —
        the model is typed ``dict | None``. The raw body is always preserved.
    """
    if not body:
        return None
    if content_type is None:
        return None
    if "json" not in content_type.lower():
        return None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


async def _dispatch_safely(
    manager: HandlerManager, webhook: StoredWebhook
) -> None:
    """Run handler dispatch and never let it crash the background task.

    Handler exceptions are already captured by the manager; this extra layer
    catches catastrophic failures (e.g. storage gone during persistence) so
    the background task pool doesn't see an unhandled exception.

    Args:
        manager: The handler manager.
        webhook: The just-stored webhook to dispatch.
    """
    try:
        await manager.dispatch(webhook)
    except Exception as exc:  # noqa: BLE001 — last-resort guard for background path
        logger.error(
            "handler_dispatch_task_crashed",
            webhook_id=webhook.id,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def build_router(
    storage: StorageBackend,
    manager: HandlerManager,
    validators: dict[str, SignatureValidator] | None = None,
    *,
    tunnel: TunnelProvider | None = None,
) -> APIRouter:
    """Construct an :class:`APIRouter` bound to a storage backend + handler manager.

    Args:
        storage: The backend used to persist webhooks. Must already be
            connected.
        manager: The handler manager that will dispatch after each store.
        validators: Optional path -> :class:`SignatureValidator` map. When
            provided, the route looks up the request's full path in this
            map; on hit, the validator runs and a 401 is returned on
            mismatch. When ``None`` or empty, no validation happens.
        tunnel: Optional :class:`TunnelProvider`. When provided, ``/healthz``
            includes a ``tunnel`` block with status + public_url +
            last_checked + error. When ``None``, the ``tunnel`` block is
            omitted from the response.

    Returns:
        A configured :class:`APIRouter`.
    """
    router = APIRouter()
    validator_map = validators or {}

    @router.post("/webhook/{path:path}")
    async def receive_webhook(
        path: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> Response:
        """Receive a webhook, persist it, optionally validate, dispatch handlers.

        Args:
            path: The sub-path captured after ``/webhook/``.
            request: The incoming Starlette/FastAPI request.
            background_tasks: FastAPI background-task queue for handler dispatch.

        Returns:
            200 on success (or no validator). 401 if a validator was
            configured for the path and rejected the signature.
        """
        body = await request.body()
        headers = _normalise_headers(request.headers.raw)
        body_json = _parse_body_json(body, headers.get("content-type"))

        source_ip: str | None = request.client.host if request.client else None
        full_path = f"/webhook/{path}" if path else "/webhook"

        webhook = WebhookRequest(
            path=full_path,
            method=request.method,
            headers=headers,
            body=body,
            body_json=body_json,
            received_at=datetime.now(UTC),
            source_ip=source_ip,
        )

        # Store-first invariant: persist BEFORE running the validator and
        # BEFORE any handler dispatch. If storage raises, we let it propagate
        # — the framework's job is to not lose webhooks silently. A 5xx is
        # preferable to a 200 with no record.
        stored = await storage.store(webhook)

        logger.info(
            "webhook_received",
            webhook_id=stored.id,
            path=full_path,
            source_ip=source_ip,
            body_bytes=len(body),
            body_json_parsed=body_json is not None,
        )

        # Signature validation (Sprint 3). Validator exceptions are NOT caught
        # here — a crashing validator is a code bug; surfacing the 5xx is
        # debuggable, and the webhook is already durably stored.
        validator = validator_map.get(full_path)
        if validator is not None:
            valid = validator.validate(body, headers)
            validator_name = type(validator).__name__
            await storage.update_signature_status(stored.id, valid, validator_name)
            # Reflect the update on the in-memory object passed to handlers.
            stored = stored.model_copy(
                update={
                    "signature_valid": valid,
                    "signature_validator": validator_name,
                }
            )
            if not valid:
                logger.warning(
                    "webhook_signature_invalid",
                    webhook_id=stored.id,
                    path=full_path,
                    validator=validator_name,
                )
                # 401, but the row stays in `webhooks` for debugging.
                return JSONResponse(
                    content={
                        "status": "rejected",
                        "id": stored.id,
                        "reason": "invalid_signature",
                    },
                    status_code=401,
                )
            logger.info(
                "webhook_signature_valid",
                webhook_id=stored.id,
                validator=validator_name,
            )

        # Dispatch handlers AFTER the response is sent. FastAPI runs
        # BackgroundTasks once the response has been delivered, so we never
        # block the client on handler latency. Handler isolation lives inside
        # the manager — by the time we get here, dispatch is fire-and-forget
        # from the route's perspective.
        background_tasks.add_task(_dispatch_safely, manager, stored)

        return JSONResponse(
            content={"status": "received", "id": stored.id},
            status_code=200,
        )

    @router.get("/healthz")
    async def healthz() -> Response:
        """Liveness probe.

        Returns:
            JSON with ``status: ok`` and the timestamp of the most recently
            stored webhook (or ``None`` if no webhooks have been received).
            If a tunnel was wired in, includes a ``tunnel`` block with
            current health (status / public_url / last_checked / error).
        """
        last = await storage.last_received_at()
        body: dict[str, object] = {
            "status": "ok",
            "last_webhook_at": last.isoformat() if last is not None else None,
        }
        if tunnel is not None:
            health = await tunnel.health()
            body["tunnel"] = {
                "status": health.status.value,
                "public_url": health.public_url,
                "last_checked": health.last_checked.isoformat(),
                "error": health.error,
            }
        return JSONResponse(content=body, status_code=200)

    return router
