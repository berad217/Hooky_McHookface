"""Tests for build_tunnel (config -> provider)."""

from __future__ import annotations

import pytest

from hooky.config.schema import CloudflareConfig, NgrokConfig, TunnelConfig
from hooky.tunnel.base import TunnelError
from hooky.tunnel.cloudflare import CloudflareProvider
from hooky.tunnel.ngrok import NgrokProvider
from hooky.tunnel.none_provider import NoTunnelProvider
from hooky.tunnel.registry import build_tunnel


def test_none_provider_built_when_no_env_needed() -> None:
    """provider='none' (the default) needs no env."""
    cfg = TunnelConfig()  # provider defaults to "none"
    t = build_tunnel(cfg, env={})
    assert isinstance(t, NoTunnelProvider)


def test_ngrok_provider_built_with_env_token() -> None:
    """NgrokProvider built when NGROK_AUTH_TOKEN is set."""
    cfg = TunnelConfig(provider="ngrok", ngrok=NgrokConfig(region="eu"))
    t = build_tunnel(cfg, env={"NGROK_AUTH_TOKEN": "tok"})
    assert isinstance(t, NgrokProvider)


def test_ngrok_missing_token_raises() -> None:
    cfg = TunnelConfig(provider="ngrok")
    with pytest.raises(TunnelError, match="NGROK_AUTH_TOKEN"):
        build_tunnel(cfg, env={})


def test_cloudflare_provider_built_with_env_token_and_hostname() -> None:
    cfg = TunnelConfig(
        provider="cloudflare",
        cloudflare=CloudflareConfig(hostname="hooky.example.com"),
    )
    t = build_tunnel(cfg, env={"CLOUDFLARED_TOKEN": "tok"})
    assert isinstance(t, CloudflareProvider)


def test_cloudflare_accepts_legacy_tunnel_name() -> None:
    """SPEC's original field name 'tunnel_name' still works."""
    cfg = TunnelConfig(
        provider="cloudflare",
        cloudflare=CloudflareConfig(tunnel_name="hooky-main.example.com"),
    )
    t = build_tunnel(cfg, env={"CLOUDFLARED_TOKEN": "tok"})
    assert isinstance(t, CloudflareProvider)


def test_cloudflare_missing_token_raises() -> None:
    cfg = TunnelConfig(
        provider="cloudflare",
        cloudflare=CloudflareConfig(hostname="x"),
    )
    with pytest.raises(TunnelError, match="CLOUDFLARED_TOKEN"):
        build_tunnel(cfg, env={})


def test_cloudflare_missing_hostname_raises() -> None:
    cfg = TunnelConfig(provider="cloudflare")  # no hostname/tunnel_name
    with pytest.raises(TunnelError, match="hostname"):
        build_tunnel(cfg, env={"CLOUDFLARED_TOKEN": "tok"})
