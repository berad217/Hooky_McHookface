"""Webhook replay engine.

Replay re-dispatches a previously stored webhook to handlers. The hard rule
(SPEC §5 + onboarding): **replay reuses the exact same
:meth:`HandlerManager.dispatch` path as live receive.** There is no
separate "replay dispatch" code — that's how we guarantee replay behaves
identically to the original delivery (isolation, concurrency, persistence
to ``handler_runs`` all included).

Handler subsetting (``--handler foo --handler bar``) is implemented by
constructing a *temporary* manager containing only the named handlers and
calling its ``dispatch``. The dispatch logic itself is untouched.

Replays produce new ``handler_runs`` rows (they are dispatched through a
storage-backed manager). We deliberately do NOT mutate the original
webhook row or tag rows as "replayed" — handler_runs rows are naturally
ordered by ``started_at``, so a replay is just a later run. This is the
opposite of the old codebase's "merge results back onto the webhook row"
approach, which conflated original and replayed outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass

from hooky.handlers.manager import HandlerManager
from hooky.log import get_logger
from hooky.storage.base import StorageBackend
from hooky.storage.models import HandlerResult, StoredWebhook

logger = get_logger(__name__)


class ReplayError(Exception):
    """Raised when a replay cannot proceed.

    Causes: the webhook id doesn't exist, or a requested handler name isn't
    registered on the manager.
    """


@dataclass
class ReplayResult:
    """Outcome of a replay.

    Attributes:
        webhook: The stored webhook that was replayed.
        results: One :class:`HandlerResult` per dispatched handler, in the
            manager's registration order (or the order requested when
            subsetting).
    """

    webhook: StoredWebhook
    results: list[HandlerResult]


async def replay_webhook(
    storage: StorageBackend,
    manager: HandlerManager,
    webhook_id: str,
    handler_names: list[str] | None = None,
) -> ReplayResult:
    """Replay a stored webhook through the handler manager.

    Args:
        storage: Backend to load the webhook from (and that the manager
            records new ``handler_runs`` to).
        manager: The fully-populated handler manager (all candidate
            handlers registered).
        webhook_id: ULID of the webhook to replay.
        handler_names: Optional subset of handler names to dispatch to. When
            ``None``, all registered handlers run. When given, only those
            handlers run — but via the same dispatch path.

    Returns:
        A :class:`ReplayResult` with the webhook + per-handler results.

    Raises:
        ReplayError: If the webhook id is unknown, or any requested handler
            name is not registered.
    """
    webhook = await storage.get(webhook_id)
    if webhook is None:
        raise ReplayError(f"Webhook not found: {webhook_id!r}")

    if handler_names is None:
        dispatch_manager = manager
    else:
        available = {h.name: h for h in manager.handlers}
        missing = [name for name in handler_names if name not in available]
        if missing:
            known = sorted(available)
            raise ReplayError(
                f"Unknown handler(s): {missing}. Registered handlers: {known}."
            )
        # Build a temporary manager with ONLY the requested handlers, sharing
        # the same storage so runs are still recorded. dispatch() is identical.
        dispatch_manager = HandlerManager(storage=storage)
        for name in handler_names:
            dispatch_manager.register(available[name])

    logger.info(
        "replay_started",
        webhook_id=webhook_id,
        path=webhook.path,
        handler_count=len(dispatch_manager.handlers),
        subset=handler_names is not None,
    )

    results = await dispatch_manager.dispatch(webhook)

    logger.info(
        "replay_complete",
        webhook_id=webhook_id,
        dispatched=len(results),
        succeeded=sum(1 for r in results if r.success),
    )

    return ReplayResult(webhook=webhook, results=results)
