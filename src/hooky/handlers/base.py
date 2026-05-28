"""Webhook handler abstract base class.

The :class:`WebhookHandler` ABC is the public contract for handler authors.
Subclassing surface is intentionally tiny:

- a ``name: str`` property (used for logging + ``handler_runs`` records)
- an ``async def process(self, webhook: StoredWebhook) -> Any`` method

That's it. No required ``__init__`` shape, no lifecycle hooks in v1, no
configure step (handler authors compose configuration into their own
constructor as they see fit).

Handlers run concurrently and in isolation via :class:`HandlerManager.dispatch`;
exceptions raised from :meth:`process` are caught by the manager, recorded as a
failed :class:`HandlerResult`, and never affect sibling handlers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from fastapi import APIRouter

from hooky.storage.models import StoredWebhook


class WebhookHandler(ABC):
    """Abstract base class for async webhook handlers.

    Concrete handlers implement :attr:`name` and :meth:`process`. The return
    value of :meth:`process` is captured in the ``return_value`` field of the
    :class:`~hooky.storage.models.HandlerResult` row produced by the manager;
    it must be JSON-serialisable if non-``None`` (the manager will serialise
    via ``json.dumps`` when persisting).

    Example:
        class PingHandler(WebhookHandler):
            @property
            def name(self) -> str:
                return "ping"

            async def process(self, webhook: StoredWebhook) -> dict:
                return {"received_id": webhook.id}
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique handler name.

        Used as the ``handler_name`` column in ``handler_runs`` and to
        disambiguate handlers in logs and replay. Two handlers with the same
        name cannot be registered against a single manager — the manager
        rejects duplicates at :meth:`HandlerManager.register` time.

        Returns:
            A short, stable identifier (e.g. ``"echo"``, ``"quo_sms"``).
        """

    @abstractmethod
    async def process(self, webhook: StoredWebhook) -> Any:
        """Process a stored webhook.

        Implementations should be async-native; if you must call sync code,
        push it through ``asyncio.to_thread`` rather than blocking the event
        loop.

        Args:
            webhook: The webhook that was just stored (or, on replay, a
                previously stored webhook).

        Returns:
            Any JSON-serialisable value. Persisted in ``handler_runs.return_value``.

        Raises:
            Exception: anything raised propagates to the manager, which catches
                it, logs the traceback, and records a failed handler run. Other
                handlers in the same dispatch are unaffected.
        """

    def routers(self) -> list[APIRouter]:
        """Optional auxiliary HTTP routes this handler contributes to the app.

        Default: none. Override to mount handler-owned routes (e.g. a read-only
        status page) on the same FastAPI app as the webhook receiver. Collected
        **once at app-build time** by :func:`hooky.app.create_app` and mounted
        as-is; they run **outside** the store-first webhook path and the
        dispatch isolation model.

        This is an escape hatch, not a handler's primary job. Constraints:

        - **Keep them read-only.** Anything that mutates state belongs behind the
          webhook path, not a side route.
        - **Own their auth.** The framework mounts these verbatim; it does not
          authenticate them. A route exposing sensitive data must gate itself
          (e.g. require an upstream identity header / verify a JWT).
        - A *router-only provider* (a handler that exists solely to contribute a
          route) may return its routers here and implement :meth:`process` as a
          no-op.

        A handler that raises from this method does not sink the app: the factory
        logs and skips it, so webhook ingest still comes up (Hooky never drops a
        webhook for an auxiliary-route bug).

        Returns:
            A list of :class:`fastapi.APIRouter` to mount. Empty by default.
        """
        return []
