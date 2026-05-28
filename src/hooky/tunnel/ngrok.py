"""Ngrok tunnel provider, backed by pyngrok.

``pyngrok`` is an optional dependency (extras = ``ngrok``). It is imported
lazily inside the provider so that:

- Users who don't use ngrok don't need it installed.
- Tests can inject a mocked ``pyngrok`` module via the constructor.

The pyngrok API is synchronous; calls are dispatched to a worker thread
via ``asyncio.to_thread`` to keep the event loop free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hooky.log import get_logger
from hooky.tunnel.base import (
    TunnelError,
    TunnelHealth,
    TunnelProvider,
    TunnelStatus,
    _utcnow,
)

logger = get_logger(__name__)


def _load_pyngrok() -> tuple[Any, Any, Any]:
    """Import pyngrok lazily and return ``(ngrok, conf, PyngrokError)``.

    Lazy import lets the module be imported without pyngrok installed —
    only callers who actually try to open a tunnel will hit the ImportError.

    Raises:
        TunnelError: If pyngrok is not installed.
    """
    try:
        from pyngrok import conf, ngrok  # type: ignore[import-not-found]
        from pyngrok.exception import PyngrokError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TunnelError(
            "pyngrok is not installed. Install with: pip install 'hooky-mchookface[ngrok]'."
        ) from exc
    return ngrok, conf, PyngrokError


class NgrokProvider(TunnelProvider):
    """Open an ngrok HTTPS tunnel to the local server.

    Args:
        auth_token: Ngrok auth token. Required (free or paid tier). Empty
            strings are rejected at construction.
        region: Ngrok region (us, eu, ap, au, sa, jp, in).
        subdomain: Optional custom subdomain (requires paid plan). If the
            plan doesn't support it, ngrok will return an error at
            ``start()`` time — we let that propagate as :class:`TunnelError`.
        pyngrok_module: Optional pre-imported pyngrok module *tuple*
            ``(ngrok, conf, PyngrokError)`` for dependency injection in
            tests. Production callers leave this ``None`` and the lazy
            import does the right thing.
    """

    def __init__(
        self,
        auth_token: str,
        region: str = "us",
        subdomain: str | None = None,
        *,
        pyngrok_module: tuple[Any, Any, Any] | None = None,
    ) -> None:
        if not auth_token:
            raise ValueError("NgrokProvider requires a non-empty auth_token.")
        self._auth_token = auth_token
        self._region = region
        self._subdomain = subdomain
        self._injected_pyngrok = pyngrok_module

        self._url: str | None = None
        self._tunnel: Any = None
        self._last_error: str | None = None

    def _pyngrok(self) -> tuple[Any, Any, Any]:
        """Return (ngrok, conf, PyngrokError), respecting test injection."""
        if self._injected_pyngrok is not None:
            return self._injected_pyngrok
        return _load_pyngrok()

    async def start(self, port: int) -> str:
        """Open the tunnel and return the public HTTPS URL.

        Idempotent: calling twice returns the same URL without opening a
        second tunnel.

        Args:
            port: Local HTTP port to forward to.

        Returns:
            Public HTTPS URL.

        Raises:
            TunnelError: If pyngrok raises, or any other error during
                tunnel setup. The original exception chains via ``__cause__``.
        """
        if self._url is not None:
            return self._url

        import asyncio

        ngrok, conf, PyngrokError = self._pyngrok()

        def _open() -> str:
            ngrok.set_auth_token(self._auth_token)
            conf.get_default().region = self._region

            options: dict[str, Any] = {"addr": port, "bind_tls": True}
            if self._subdomain:
                options["subdomain"] = self._subdomain

            tunnel = ngrok.connect(**options)
            self._tunnel = tunnel
            url: str = tunnel.public_url
            # Force HTTPS — pyngrok sometimes returns http:// on older versions.
            if url.startswith("http://"):
                url = "https://" + url[len("http://") :]
            return url

        try:
            self._url = await asyncio.to_thread(_open)
            self._last_error = None
            logger.info("ngrok_tunnel_started", public_url=self._url, region=self._region)
            return self._url
        except PyngrokError as exc:
            self._last_error = f"PyngrokError: {exc}"
            logger.error("ngrok_tunnel_start_failed", error=str(exc))
            raise TunnelError(f"Failed to open ngrok tunnel: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — wrap unexpected errors too
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "ngrok_tunnel_start_failed_unexpected",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise TunnelError(f"Unexpected error opening ngrok tunnel: {exc}") from exc

    async def stop(self) -> None:
        """Tear down the tunnel and kill the ngrok process. Idempotent.

        Disconnect errors are logged and swallowed — a tunnel that's
        already gone is "stopped."
        """
        if self._tunnel is None:
            return

        import asyncio

        ngrok, _conf, PyngrokError = self._pyngrok()
        url = self._url

        def _close() -> None:
            try:
                if url is not None:
                    ngrok.disconnect(url)
            except PyngrokError as exc:
                logger.warning("ngrok_disconnect_failed", error=str(exc))
            try:
                ngrok.kill()
            except PyngrokError as exc:
                logger.warning("ngrok_kill_failed", error=str(exc))

        await asyncio.to_thread(_close)
        self._tunnel = None
        self._url = None
        logger.info("ngrok_tunnel_stopped")

    async def health(self) -> TunnelHealth:
        """Return cached state. No network call.

        Returns:
            :class:`TunnelHealth` based on last-known state. If a previous
            ``start()`` failed, surfaces ``ERROR`` with the cached error.
        """
        if self._url is not None:
            return TunnelHealth(
                status=TunnelStatus.CONNECTED,
                public_url=self._url,
                last_checked=_utcnow(),
            )
        if self._last_error is not None:
            return TunnelHealth(
                status=TunnelStatus.ERROR,
                public_url=None,
                last_checked=_utcnow(),
                error=self._last_error,
            )
        return TunnelHealth(
            status=TunnelStatus.DISCONNECTED,
            public_url=None,
            last_checked=_utcnow(),
        )


# Mapping helper imported lazily by registry — kept here to avoid a circular import.
def _build_from_config(
    cfg: Mapping[str, Any], *, env: Mapping[str, str]
) -> NgrokProvider:
    """Construct an :class:`NgrokProvider` from the ``tunnel.ngrok`` config block.

    Args:
        cfg: The ``ngrok:`` sub-block from config (region, subdomain).
        env: Environment mapping. ``NGROK_AUTH_TOKEN`` must be set.

    Returns:
        Configured :class:`NgrokProvider`.

    Raises:
        TunnelError: If ``NGROK_AUTH_TOKEN`` is missing.
    """
    auth_token = env.get("NGROK_AUTH_TOKEN", "")
    if not auth_token:
        raise TunnelError(
            "Tunnel provider 'ngrok' requires env var NGROK_AUTH_TOKEN to be set."
        )
    return NgrokProvider(
        auth_token=auth_token,
        region=cfg.get("region", "us"),
        subdomain=cfg.get("subdomain"),
    )
