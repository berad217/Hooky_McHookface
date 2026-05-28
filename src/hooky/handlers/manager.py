"""Handler registration + concurrent isolated dispatch.

:class:`HandlerManager` is the runtime glue between a stored webhook and the
handlers that should react to it. The design rules per SPEC §5/§7 Sprint 2:

- Concurrent: every registered handler runs in parallel via
  ``asyncio.gather(..., return_exceptions=True)``.
- Isolated: an exception in one handler never affects another. The exception
  is caught, the traceback logged, and a failed :class:`HandlerResult`
  recorded.
- Persistent: each run produces a row in the ``handler_runs`` table via
  :meth:`StorageBackend.record_handler_run`. Storage failures here are caught
  + logged but never re-raised — the dispatch contract is "best-effort,
  always returns a result per handler".
- Replay-compatible: Sprint 5's replay engine reuses this exact method, so
  the same code path runs live receive and manual replay.

The manager intentionally does *not* time handlers out in v1 — a stuck
handler blocks its own slot, never anyone else's, and a hung dispatch is
debuggable via the absence of a ``completed_at`` row.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import UTC, datetime
from typing import Any

from hooky.handlers.base import WebhookHandler
from hooky.log import get_logger
from hooky.storage.base import StorageBackend
from hooky.storage.models import HandlerResult, StoredWebhook

logger = get_logger(__name__)


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime`` (test-overridable).

    Returns:
        Current UTC time as a tz-aware :class:`datetime`.
    """
    return datetime.now(UTC)


def _is_json_serialisable(value: Any) -> bool:
    """Best-effort check that ``value`` can be ``json.dumps``-ed.

    Args:
        value: Candidate return value from a handler.

    Returns:
        ``True`` if ``json.dumps(value)`` would succeed, else ``False``.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


class HandlerManager:
    """Registers handlers and dispatches webhooks concurrently with isolation.

    The manager is the single integration seam between the route layer and
    handler authors. The route holds a reference to one manager per app; the
    manager owns the registered handler list and the dispatch policy.

    Args:
        storage: Backend used to persist :class:`HandlerResult` rows. ``None``
            disables persistence (used by lightweight tests and any future
            replay-without-record mode). When ``None``, dispatch still returns
            results — they're simply not written to ``handler_runs``.
    """

    def __init__(self, storage: StorageBackend | None = None) -> None:
        self._handlers: list[WebhookHandler] = []
        self._storage = storage

    # -- registration ----------------------------------------------------

    def register(self, handler: WebhookHandler) -> None:
        """Register a handler.

        Args:
            handler: The handler to register. Must have a unique ``name``.

        Raises:
            ValueError: If a handler with the same ``name`` is already
                registered, or if ``handler.name`` is empty.
        """
        if not handler.name:
            raise ValueError("Handler.name must be a non-empty string.")
        if any(h.name == handler.name for h in self._handlers):
            raise ValueError(f"Handler {handler.name!r} is already registered.")
        self._handlers.append(handler)
        logger.info("handler_registered", handler_name=handler.name)

    @property
    def handlers(self) -> list[WebhookHandler]:
        """Return a snapshot of currently-registered handlers.

        Returns:
            A new list (mutating the result does not affect the manager).
        """
        return list(self._handlers)

    def __len__(self) -> int:
        return len(self._handlers)

    # -- dispatch --------------------------------------------------------

    async def dispatch(self, webhook: StoredWebhook) -> list[HandlerResult]:
        """Run every registered handler against ``webhook`` concurrently.

        All handlers are kicked off in parallel via ``asyncio.gather`` with
        ``return_exceptions=True``. Each handler's outcome becomes one
        :class:`HandlerResult`. Results are persisted via
        ``storage.record_handler_run`` if storage was provided to the manager.

        The order of the returned list matches registration order, so callers
        (notably the replay engine) can correlate handlers and results without
        an extra lookup.

        Args:
            webhook: The stored webhook to dispatch.

        Returns:
            One :class:`HandlerResult` per registered handler, in registration
            order. Always returns; never raises (handler exceptions are
            captured as failed results).
        """
        if not self._handlers:
            return []

        coros = [self._run_one(handler, webhook) for handler in self._handlers]
        # return_exceptions=True is a belt-and-braces guard. _run_one already
        # catches every exception from handler code, so in practice gather
        # should not see a raised exception; we keep return_exceptions on so
        # an unexpected bug in _run_one cannot poison the whole dispatch.
        raw = await asyncio.gather(*coros, return_exceptions=True)

        results: list[HandlerResult] = []
        for handler, item in zip(self._handlers, raw, strict=True):
            if isinstance(item, HandlerResult):
                results.append(item)
            else:
                # _run_one itself blew up — synthesise a failed result so the
                # caller still gets one row per handler.
                exc = item if isinstance(item, BaseException) else RuntimeError(repr(item))
                tb = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
                now = _utcnow()
                results.append(
                    HandlerResult(
                        handler_name=handler.name,
                        webhook_id=webhook.id,
                        started_at=now,
                        completed_at=now,
                        success=False,
                        return_value=None,
                        error=f"{type(exc).__name__}: {exc}",
                        traceback=tb,
                    )
                )
                logger.error(
                    "handler_dispatch_unexpected_error",
                    handler_name=handler.name,
                    webhook_id=webhook.id,
                    error=str(exc),
                )

        if self._storage is not None:
            await self._persist_results(results)

        return results

    # -- internals -------------------------------------------------------

    async def _run_one(
        self, handler: WebhookHandler, webhook: StoredWebhook
    ) -> HandlerResult:
        """Run a single handler and produce a :class:`HandlerResult`.

        Exceptions from :meth:`WebhookHandler.process` are caught here. The
        manager itself never re-raises handler errors — that's the whole
        point of the isolation invariant.

        Args:
            handler: The handler to run.
            webhook: The webhook to pass to :meth:`WebhookHandler.process`.

        Returns:
            A :class:`HandlerResult` capturing success/failure, timings, and
            either the return value or the error + traceback.
        """
        started_at = _utcnow()
        try:
            return_value = await handler.process(webhook)
        except Exception as exc:  # noqa: BLE001 — deliberate broad catch for isolation
            completed_at = _utcnow()
            tb = traceback.format_exc()
            logger.error(
                "handler_raised",
                handler_name=handler.name,
                webhook_id=webhook.id,
                error=str(exc),
                error_type=type(exc).__name__,
                traceback=tb,
            )
            return HandlerResult(
                handler_name=handler.name,
                webhook_id=webhook.id,
                started_at=started_at,
                completed_at=completed_at,
                success=False,
                return_value=None,
                error=f"{type(exc).__name__}: {exc}",
                traceback=tb,
            )

        completed_at = _utcnow()
        # Drop non-JSON-serialisable returns rather than failing the run —
        # handler authors should be able to return rich objects in-process
        # for tests/replay even if those don't round-trip through SQLite.
        persisted_value: Any | None = return_value if _is_json_serialisable(return_value) else None
        latency_ms = (completed_at - started_at).total_seconds() * 1000.0
        logger.info(
            "handler_completed",
            handler_name=handler.name,
            webhook_id=webhook.id,
            latency_ms=round(latency_ms, 3),
            outcome="success",
        )
        return HandlerResult(
            handler_name=handler.name,
            webhook_id=webhook.id,
            started_at=started_at,
            completed_at=completed_at,
            success=True,
            return_value=persisted_value,
            error=None,
            traceback=None,
        )

    async def _persist_results(self, results: list[HandlerResult]) -> None:
        """Write each result to ``handler_runs``, swallowing storage errors.

        A failure to record a handler run must not break the live request
        path — by the time we get here the route has already returned 200.
        We log and continue.

        Args:
            results: The results to persist.
        """
        assert self._storage is not None  # narrowed by caller
        for result in results:
            try:
                await self._storage.record_handler_run(result)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "handler_run_persist_failed",
                    handler_name=result.handler_name,
                    webhook_id=result.webhook_id,
                    error=str(exc),
                )
