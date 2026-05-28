"""Logger reference handler.

Logs each received webhook at INFO via the framework's structlog logger.
Body and headers are NOT logged by default — we want a useful "I saw a
webhook" trail without spraying potentially sensitive payloads to stdout.
Handler authors who want bodies logged can subclass or use their own
handler.

Per SPEC §7 Sprint 2: "LoggerHandler logs the webhook at INFO."
"""

from __future__ import annotations

from typing import Any

from hooky.handlers.base import WebhookHandler
from hooky.log import get_logger
from hooky.storage.models import StoredWebhook

_log = get_logger(__name__)


class LoggerHandler(WebhookHandler):
    """Handler that logs a one-line summary of each webhook at INFO."""

    @property
    def name(self) -> str:
        """Handler name (constant)."""
        return "logger"

    async def process(self, webhook: StoredWebhook) -> dict[str, Any]:
        """Log a summary line and return a small acknowledgement dict.

        Args:
            webhook: The stored webhook to log.

        Returns:
            A dict acknowledging the log emission (persisted to
            ``handler_runs.return_value``).
        """
        _log.info(
            "webhook_logged",
            webhook_id=webhook.id,
            path=webhook.path,
            method=webhook.method,
            body_bytes=len(webhook.body),
            body_json_parsed=webhook.body_json is not None,
            source_ip=webhook.source_ip,
            signature_valid=webhook.signature_valid,
        )
        return {
            "logged": True,
            "webhook_id": webhook.id,
        }
