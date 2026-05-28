"""Tunnel provider abstract base class + health model.

The :class:`TunnelProvider` ABC is the public contract for tunnel
providers. Three operations:

- ``async start() -> str`` — open the tunnel; return the public URL the
  internet should hit. Idempotent (calling twice returns the same URL).
- ``async stop() -> None`` — tear it down. Idempotent (calling on an
  already-stopped tunnel is a no-op).
- ``async health() -> TunnelHealth`` — best-effort liveness snapshot.
  Surfaced in ``/healthz``.

The whole subsystem is async-native: providers that wrap synchronous
libraries (e.g. pyngrok) MUST run those calls via ``asyncio.to_thread``
rather than blocking the event loop. Providers that drive subprocesses
(cloudflared) use ``asyncio.create_subprocess_exec`` directly.

Lifecycle is managed by FastAPI's ``lifespan`` in :mod:`hooky.app`:
``start()`` on app startup, ``stop()`` on shutdown.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class TunnelStatus(StrEnum):
    """Coarse-grained tunnel state used by ``/healthz`` and ops dashboards."""

    DISCONNECTED = "disconnected"
    """Tunnel has not been started, or has been cleanly stopped."""

    CONNECTED = "connected"
    """Tunnel is up and the public URL should be reachable."""

    ERROR = "error"
    """Tunnel attempted to start but failed, or crashed while running."""


class TunnelHealth(BaseModel):
    """A health snapshot for a tunnel.

    Attributes:
        status: Coarse state — disconnected / connected / error.
        public_url: The public URL when ``status == CONNECTED``, else
            ``None``. Included in health output so ``/healthz`` consumers
            don't need a second call to discover it.
        last_checked: When this snapshot was taken (tz-aware UTC).
        error: Human-readable error string when ``status == ERROR``. ``None``
            otherwise. Implementations should keep this short — full
            tracebacks belong in logs.
    """

    model_config = ConfigDict(use_enum_values=False)

    status: TunnelStatus
    public_url: str | None = None
    last_checked: datetime
    error: str | None = None


class TunnelError(RuntimeError):
    """Raised by :meth:`TunnelProvider.start` when the tunnel cannot come up.

    Providers wrap underlying library / subprocess errors in
    :class:`TunnelError` so the app factory (and operators reading logs)
    have a stable exception type to catch.
    """


class TunnelProvider(ABC):
    """Async tunnel provider.

    Concrete providers (``NoTunnelProvider``, ``NgrokProvider``,
    ``CloudflareProvider``) implement the three methods. They each carry
    their own configuration via ``__init__`` — there is no shared
    configure-step. The local server port is passed to ``start()`` because
    providers may not know it at construction time (the app factory wires
    it in from config).

    Implementations MUST be idempotent on both start and stop:

    - Calling ``start()`` twice without ``stop()`` in between should not
      open two tunnels; the second call returns the same URL as the first.
    - Calling ``stop()`` on a never-started or already-stopped tunnel is a
      no-op (no exception).

    This matches FastAPI's lifespan semantics, where shutdown may be called
    after a failed startup.
    """

    @abstractmethod
    async def start(self, port: int) -> str:
        """Open the tunnel and return the public URL.

        Args:
            port: Local HTTP port the tunnel should forward to.

        Returns:
            The public URL (typically HTTPS) external services should POST to.

        Raises:
            TunnelError: If the tunnel cannot be brought up. The app
                factory lets this propagate so startup fails loud rather
                than silently running with no tunnel.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Tear the tunnel down. Idempotent.

        Implementations should swallow shutdown errors (a tunnel that's
        already gone is "stopped"). Logging the error is fine; raising is
        not — shutdown paths must run to completion.
        """

    @abstractmethod
    async def health(self) -> TunnelHealth:
        """Return a :class:`TunnelHealth` snapshot.

        This method MUST be cheap and non-blocking. Providers cache the
        last-known state internally and return it; they do not, for
        example, make a network round-trip to verify connectivity on
        every ``/healthz`` request.

        Returns:
            A fresh :class:`TunnelHealth` with the current state. The
            ``last_checked`` field reflects when this snapshot was taken
            (typically ``datetime.now(timezone.utc)``).
        """


def _utcnow() -> datetime:
    """Return a tz-aware UTC datetime. Lives here so providers share one helper."""

    return datetime.now(UTC)


__all__ = [
    "TunnelError",
    "TunnelHealth",
    "TunnelProvider",
    "TunnelStatus",
    "_utcnow",
]
