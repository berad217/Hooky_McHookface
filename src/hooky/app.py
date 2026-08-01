"""FastAPI app factory.

``create_app`` is the entry point invoked by uvicorn:

.. code-block:: bash

   uvicorn hooky.app:create_app --factory

It accepts optional injected dependencies (``storage``, ``config``,
``handler_manager``, ``validators``, ``tunnel``) so tests can wire in
temp/in-memory state without monkeypatching. Production calls
``create_app()`` with no arguments and the factory builds everything from
the loaded config; handlers are registered by the caller before invoking
``create_app`` when running as a library, or by the (Sprint 5) CLI when
running as a shipped binary.

Sprint 4 adds tunnel lifecycle management via FastAPI's ``lifespan``:
the tunnel starts on app startup and stops on shutdown. Tunnel start
failures fail loud (the app refuses to come up) — running a webhook
receiver without a tunnel is almost always a misconfiguration in
production.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from hooky.config import HookyConfig, load_config
from hooky.handlers.manager import HandlerManager
from hooky.handlers.registry import build_handler_manager
from hooky.log import configure_logging, get_logger
from hooky.routes import build_router
from hooky.storage.base import StorageBackend
from hooky.storage.sqlite import AsyncSQLiteStorage
from hooky.tunnel.base import TunnelProvider
from hooky.tunnel.registry import build_tunnel
from hooky.validators.base import SignatureValidator
from hooky.validators.registry import build_validator_map

logger = get_logger(__name__)

#: How often the retention purge re-runs while the app is up. Daily: retention
#: is expressed in days, so a finer cadence buys nothing and a coarser one lets
#: a long-lived process drift past its own policy.
_PURGE_INTERVAL_SECONDS = 24 * 60 * 60


async def _cancel(task: asyncio.Task[None]) -> None:
    """Cancel a background task and await its exit. Never raises."""
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: B014 — shutdown must not raise
        pass


def create_app(
    config: HookyConfig | None = None,
    storage: StorageBackend | None = None,
    handler_manager: HandlerManager | None = None,
    validators: dict[str, SignatureValidator] | None = None,
    tunnel: TunnelProvider | None = None,
) -> FastAPI:
    """Build a FastAPI application instance.

    Args:
        config: Optional pre-loaded :class:`HookyConfig`. If ``None``, defaults
            are used (no YAML file).
        storage: Optional pre-built storage backend. If ``None``, the factory
            constructs :class:`AsyncSQLiteStorage` from ``config.storage.path``
            and connects/closes it via the FastAPI lifespan.
        handler_manager: Optional pre-built :class:`HandlerManager`.
        validators: Optional pre-built path -> validator map. If ``None``,
            the factory constructs the map from ``config.validators``.
        tunnel: Optional pre-built :class:`TunnelProvider`. If ``None``,
            the factory constructs one from ``config.tunnel`` (defaults to
            :class:`NoTunnelProvider`). Tests inject directly.

    Returns:
        A configured :class:`FastAPI` instance ready for uvicorn.
    """
    cfg = config if config is not None else load_config()
    configure_logging(
        level=cfg.server.log_level,
        json_format=cfg.server.log_format == "json",
    )

    # Storage: caller-injected wins; otherwise build from config. If we
    # built it, we own its connect/close lifecycle.
    owns_storage = storage is None
    backend: StorageBackend = (
        storage if storage is not None else AsyncSQLiteStorage(cfg.storage.path)
    )

    # Tunnel: same pattern as storage. If we built it, lifespan starts/stops it.
    owns_tunnel = tunnel is None
    tunnel_provider: TunnelProvider = (
        tunnel if tunnel is not None else build_tunnel(cfg.tunnel)
    )

    async def _purge_once() -> None:
        """Apply ``storage.retention_days`` once. Never raises."""
        try:
            cutoff = datetime.now(UTC) - timedelta(days=cfg.storage.retention_days)
            deleted = await backend.purge_before(cutoff)
            if deleted:
                logger.info(
                    "storage_purged",
                    deleted=deleted,
                    retention_days=cfg.storage.retention_days,
                    cutoff=cutoff.isoformat(),
                )
        except Exception as exc:  # noqa: BLE001 — retention must never take the app down
            logger.error(
                "storage_purge_failed", error=str(exc), error_type=type(exc).__name__
            )

    async def _purge_loop() -> None:
        """Re-apply retention daily.

        Startup-only would be a gesture, not a fix: this process is a service
        that can run for months between restarts, so a purge that only fires at
        boot leaves an unbounded store for exactly the deployments that need it
        most. Sleeps FIRST because startup already purged.
        """
        while True:
            await asyncio.sleep(_PURGE_INTERVAL_SECONDS)
            await _purge_once()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        purge_task: asyncio.Task[None] | None = None
        if owns_storage:
            await backend.connect()
            logger.info("storage_connected", path=cfg.storage.path)

        # Retention (storage.retention_days; 0 = keep forever). The counterweight
        # to the store-first invariant: every body is persisted BEFORE the
        # validator and BEFORE handler dispatch, so rejected requests — bad
        # signature, or a sender a handler denies — are kept in full. Without
        # this they were kept forever, and a backup that replicates the DB
        # off-machine carries them along.
        if cfg.storage.retention_days > 0:
            await _purge_once()
            purge_task = asyncio.create_task(_purge_loop())

        if owns_tunnel:
            try:
                public_url = await tunnel_provider.start(cfg.server.port)
                logger.info(
                    "tunnel_started",
                    provider=type(tunnel_provider).__name__,
                    public_url=public_url,
                )
            except Exception:
                # If startup fails, still close anything we already opened —
                # including the purge task, which would otherwise keep firing
                # against a closed backend.
                if purge_task is not None:
                    await _cancel(purge_task)
                if owns_storage:
                    await backend.close()
                raise

        try:
            yield
        finally:
            # Cancel BEFORE closing storage, so a purge in flight cannot race a
            # closing connection.
            if purge_task is not None:
                await _cancel(purge_task)
            # Stop tunnel first (it depends on storage to be irrelevant, but
            # consistent order helps reasoning about shutdown).
            if owns_tunnel:
                try:
                    await tunnel_provider.stop()
                    logger.info("tunnel_stopped", provider=type(tunnel_provider).__name__)
                except Exception as exc:  # noqa: BLE001 — never block shutdown on tunnel
                    logger.error(
                        "tunnel_stop_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            if owns_storage:
                await backend.close()
                logger.info("storage_closed", path=cfg.storage.path)

    # Handler manager: caller-injected wins; otherwise build from config.
    # An empty ``handlers`` list yields an empty manager (Sprint 1-4 behavior).
    if handler_manager is not None:
        manager = handler_manager
    else:
        manager = build_handler_manager(cfg.handlers, storage=backend)
        if cfg.handlers:
            logger.info(
                "handlers_loaded_from_config",
                count=len(manager.handlers),
                names=[h.name for h in manager.handlers],
            )

    if validators is not None:
        validator_map = validators
    else:
        validator_map = build_validator_map(cfg.validators)
        if validator_map:
            logger.info(
                "validators_registered",
                count=len(validator_map),
                paths=sorted(validator_map.keys()),
            )

    app = FastAPI(
        title="Hooky_McHookface",
        version="0.1.0",
        description="Async-native webhook receiver framework.",
        lifespan=lifespan,
    )
    app.include_router(
        build_router(backend, manager, validator_map, tunnel=tunnel_provider)
    )

    # Plugin-contributed auxiliary routers (e.g. a handler's read-only status
    # page). Optional + additive. Isolated like dispatch: a handler that fails to
    # produce its routers must NOT stop the app from coming up and receiving
    # webhooks — Hooky's prime directive is to never drop a webhook — so we log
    # and skip rather than propagate. Mounted after the core router; the webhook
    # catch-all (POST /webhook/{path}) can't collide with handler GET routes.
    for handler in manager.handlers:
        try:
            extra_routers = handler.routers()
        except Exception as exc:  # noqa: BLE001 — an aux-route bug must not sink ingest
            logger.error(
                "handler_routers_failed",
                handler_name=handler.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            continue
        for extra in extra_routers:
            app.include_router(extra)
            logger.info("handler_router_mounted", handler_name=handler.name)

    # Expose for tests / introspection. Not part of the public API.
    app.state.storage = backend
    app.state.config = cfg
    app.state.handler_manager = manager
    app.state.validators = validator_map
    app.state.tunnel = tunnel_provider

    return app
