"""Build a :class:`TunnelProvider` from configuration.

Selects the concrete provider based on ``TunnelConfig.provider`` and wires
up secrets from environment variables (``NGROK_AUTH_TOKEN`` /
``CLOUDFLARED_TOKEN``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from hooky.config.schema import TunnelConfig
from hooky.tunnel.base import TunnelError, TunnelProvider
from hooky.tunnel.cloudflare import _build_from_config as _build_cloudflare
from hooky.tunnel.ngrok import _build_from_config as _build_ngrok
from hooky.tunnel.none_provider import NoTunnelProvider


def build_tunnel(
    cfg: TunnelConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> TunnelProvider:
    """Instantiate the configured :class:`TunnelProvider`.

    Args:
        cfg: Tunnel configuration block.
        env: Environment mapping for secret/token resolution. Defaults to
            ``os.environ``. Tests inject a controlled mapping.

    Returns:
        A configured :class:`TunnelProvider`.

    Raises:
        TunnelError: If required env vars or config fields are missing for
            the chosen provider.
    """
    source = env if env is not None else os.environ

    if cfg.provider == "none":
        return NoTunnelProvider()

    if cfg.provider == "ngrok":
        return _build_ngrok(cfg.ngrok.model_dump(exclude_none=True), env=source)

    if cfg.provider == "cloudflare":
        return _build_cloudflare(cfg.cloudflare.model_dump(exclude_none=True), env=source)

    # Should be unreachable thanks to TunnelProviderType Literal.
    raise TunnelError(f"Unknown tunnel provider: {cfg.provider!r}")
