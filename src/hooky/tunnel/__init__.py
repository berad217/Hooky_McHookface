"""Tunnel providers package.

Public surface:

- :class:`TunnelProvider` — the ABC providers subclass.
- :class:`TunnelHealth`, :class:`TunnelStatus`, :class:`TunnelError` — the
  health-snapshot model + status enum + provider error type.
- :class:`NoTunnelProvider`, :class:`NgrokProvider`, :class:`CloudflareProvider`
  — the providers shipped in v1.
- :func:`build_tunnel` — config-driven instantiation used by the app factory.
"""

from hooky.tunnel.base import (
    TunnelError,
    TunnelHealth,
    TunnelProvider,
    TunnelStatus,
)
from hooky.tunnel.cloudflare import CloudflareProvider
from hooky.tunnel.ngrok import NgrokProvider
from hooky.tunnel.none_provider import NoTunnelProvider
from hooky.tunnel.registry import build_tunnel

__all__ = [
    "CloudflareProvider",
    "NgrokProvider",
    "NoTunnelProvider",
    "TunnelError",
    "TunnelHealth",
    "TunnelProvider",
    "TunnelStatus",
    "build_tunnel",
]
