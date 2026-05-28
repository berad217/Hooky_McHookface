"""Storage backend abstract base class.

This ABC is the public contract for persistence. Concrete backends
(``AsyncSQLiteStorage`` in v1; future ``PostgresStorage`` / ``RedisStorage``)
implement it. Routes and the handler manager use this interface only — no
concrete-backend imports outside of construction.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import datetime

from hooky.storage.models import HandlerResult, StoredWebhook, WebhookRequest


class StorageBackend(ABC):
    """Abstract async storage backend for webhooks and handler runs.

    Lifecycle: callers must :meth:`connect` before first use and
    :meth:`close` on shutdown. Implementations should make :meth:`connect`
    idempotent so that repeat calls are safe.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open the underlying connection and ensure schema is in place.

        Idempotent: safe to call more than once.

        Raises:
            Exception: implementation-specific. Backends should let underlying
                errors propagate so the caller can react.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""

    @abstractmethod
    async def store(self, webhook: WebhookRequest) -> StoredWebhook:
        """Persist a webhook and return the stored representation.

        Implementations MUST assign a ULID id and persist the row before
        returning. This is the store-first invariant — callers depend on it.

        Args:
            webhook: The in-flight webhook to persist.

        Returns:
            The stored webhook, with an ``id`` assigned.
        """

    @abstractmethod
    async def get(self, webhook_id: str) -> StoredWebhook | None:
        """Retrieve a stored webhook by id.

        Args:
            webhook_id: ULID assigned at store time.

        Returns:
            The stored webhook, or ``None`` if not found.
        """

    @abstractmethod
    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        path: str | None = None,
    ) -> list[StoredWebhook]:
        """List stored webhooks, most recent first.

        Args:
            limit: Maximum number of rows to return.
            offset: Number of rows to skip (for pagination).
            path: If given, return only webhooks whose ``path`` matches
                exactly. ``None`` returns all paths.

        Returns:
            A list of stored webhooks ordered by ``received_at`` descending.
        """

    @abstractmethod
    # builtins.list: the `list` method above shadows the `list` builtin in this
    # class's annotation scope under `from __future__ import annotations`.
    async def get_handler_runs(self, webhook_id: str) -> builtins.list[HandlerResult]:
        """Return all recorded handler runs for a webhook, oldest first.

        Used by ``hooky show`` to display per-handler outcomes (including
        any later replays, which appear as additional rows).

        Args:
            webhook_id: ULID of the webhook.

        Returns:
            A list of :class:`HandlerResult`, ordered by ``started_at``
            ascending. Empty if no handlers have run against this webhook.
        """

    @abstractmethod
    async def last_received_at(self) -> datetime | None:
        """Return the timestamp of the most recently stored webhook.

        Returns:
            The latest ``received_at`` value, or ``None`` if the store is empty.
        """

    @abstractmethod
    async def record_handler_run(self, result: HandlerResult) -> None:
        """Persist a handler run result.

        Sprint 1 does not invoke this method (no handlers yet) but the ABC
        carries it so concrete backends ship the table from day 1.

        Args:
            result: The handler result to record.
        """

    @abstractmethod
    async def update_signature_status(
        self,
        webhook_id: str,
        valid: bool,
        validator_name: str,
    ) -> None:
        """Update the signature validation status for a stored webhook.

        Called by the route layer after a validator runs. The webhook row
        already exists (store-first invariant); this method updates the
        ``signature_valid`` and ``signature_validator`` columns in place.

        Args:
            webhook_id: ULID of the previously stored webhook.
            valid: ``True`` if the signature validated, ``False`` otherwise.
                Invalid webhooks are kept (for debugging) but the route
                returns 401 and skips handler dispatch.
            validator_name: Identifier of the validator that ran (typically
                the validator class name, e.g. ``"GitHubValidator"``).

        Raises:
            Exception: Implementation-specific. The route lets storage
                errors propagate so a sig-update failure surfaces as 5xx
                rather than a silent inconsistency.
        """
