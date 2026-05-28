"""No-op tunnel provider — returns the local URL.

Useful for local development, CI, and as the default when no real tunnel
is configured. ``start()`` does no I/O and never fails.

Module is named ``none_provider`` (not ``none``) to avoid shadowing the
``None`` keyword at import-time. The exposed class name remains
``NoTunnelProvider``.
"""

from __future__ import annotations

from hooky.tunnel.base import TunnelHealth, TunnelProvider, TunnelStatus, _utcnow


class NoTunnelProvider(TunnelProvider):
    """Returns ``http://localhost:{port}`` and otherwise does nothing.

    Args:
        host: Hostname to embed in the URL. Defaults to ``"localhost"``.
            Set to e.g. ``"127.0.0.1"`` if you want a more explicit address.
    """

    def __init__(self, host: str = "localhost") -> None:
        self._host = host
        self._url: str | None = None

    async def start(self, port: int) -> str:
        """Compute and return the local URL.

        Idempotent: calling twice returns the same URL.

        Args:
            port: Local HTTP port.

        Returns:
            ``"http://{host}:{port}"``.
        """
        if self._url is None:
            self._url = f"http://{self._host}:{port}"
        return self._url

    async def stop(self) -> None:
        """Clear cached URL. No external resources to release."""
        self._url = None

    async def health(self) -> TunnelHealth:
        """Return ``CONNECTED`` whenever ``start()`` has been called.

        The local server's own reachability is reported separately by
        ``/healthz`` itself; this provider has nothing else to check.
        """
        if self._url is None:
            return TunnelHealth(
                status=TunnelStatus.DISCONNECTED,
                public_url=None,
                last_checked=_utcnow(),
            )
        return TunnelHealth(
            status=TunnelStatus.CONNECTED,
            public_url=self._url,
            last_checked=_utcnow(),
        )
