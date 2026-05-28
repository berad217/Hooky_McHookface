"""Echo reference handler.

Returns the webhook body in a structured-but-trivial dict. The point is to
have a reference handler that exercises the full receive -> store -> dispatch
-> handler_runs round-trip without depending on any external service. Useful
in tests, smoke runs, and as a worked example for handler authors.

Per SPEC §7 Sprint 2: "EchoHandler returns the webhook body."
"""

from __future__ import annotations

from typing import Any

from hooky.handlers.base import WebhookHandler
from hooky.storage.models import StoredWebhook


class EchoHandler(WebhookHandler):
    """Handler that returns the webhook body as its result.

    Returns a small dict with ``body_json`` (if present) or a UTF-8-decoded
    ``body`` string (or ``None`` if non-decodable). The structure is
    JSON-serialisable so :class:`hooky.handlers.manager.HandlerManager` can
    persist it verbatim into ``handler_runs.return_value``.
    """

    @property
    def name(self) -> str:
        """Handler name (constant)."""
        return "echo"

    async def process(self, webhook: StoredWebhook) -> dict[str, Any]:
        """Echo the webhook body back as the handler return value.

        Args:
            webhook: The stored webhook.

        Returns:
            A dict with the parsed JSON body (preferred), or a decoded text
            body, plus the originating webhook id for traceability.
        """
        body_repr: Any
        if webhook.body_json is not None:
            body_repr = webhook.body_json
        else:
            try:
                body_repr = webhook.body.decode("utf-8")
            except UnicodeDecodeError:
                body_repr = None
        return {
            "webhook_id": webhook.id,
            "body": body_repr,
        }
