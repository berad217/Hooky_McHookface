"""Tests for NgrokProvider using a mock pyngrok module.

pyngrok is an optional dependency and isn't installed in the test env.
We satisfy the constructor's ``pyngrok_module`` injection seam with a
hand-built (ngrok, conf, PyngrokError) tuple instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hooky.tunnel.base import TunnelError, TunnelStatus
from hooky.tunnel.ngrok import NgrokProvider


class _FakePyngrokError(Exception):
    """Stand-in for pyngrok.exception.PyngrokError."""


def _make_fake_pyngrok(public_url: str = "https://abc123.ngrok.io"):
    """Build (ngrok, conf, PyngrokError) with sensible defaults.

    Returns:
        Tuple shaped like the real pyngrok import surface, plus the
        underlying MagicMocks (so individual tests can override behavior
        like ``ngrok.connect.side_effect``).
    """
    ngrok = MagicMock()
    ngrok.connect.return_value = SimpleNamespace(public_url=public_url)
    ngrok.set_auth_token = MagicMock()
    ngrok.disconnect = MagicMock()
    ngrok.kill = MagicMock()

    conf_default = SimpleNamespace(region="us")
    conf = MagicMock()
    conf.get_default.return_value = conf_default

    return (ngrok, conf, _FakePyngrokError), ngrok, conf


async def test_start_returns_public_url() -> None:
    """start() returns the public_url from pyngrok."""
    pyngrok, ngrok, _conf = _make_fake_pyngrok("https://abc.ngrok.io")
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)

    url = await p.start(8000)
    assert url == "https://abc.ngrok.io"
    ngrok.set_auth_token.assert_called_once_with("tok")
    ngrok.connect.assert_called_once()
    # Verify port + HTTPS were passed
    kwargs = ngrok.connect.call_args.kwargs
    assert kwargs["addr"] == 8000
    assert kwargs["bind_tls"] is True


async def test_start_forces_https_on_http_url() -> None:
    """If pyngrok hands back http://, NgrokProvider rewrites to https://."""
    pyngrok, _ngrok, _conf = _make_fake_pyngrok("http://abc.ngrok.io")
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)

    url = await p.start(8000)
    assert url == "https://abc.ngrok.io"


async def test_start_is_idempotent() -> None:
    """A second start() returns the cached URL and does NOT call ngrok.connect again."""
    pyngrok, ngrok, _conf = _make_fake_pyngrok()
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)

    url1 = await p.start(8000)
    url2 = await p.start(8000)
    assert url1 == url2
    ngrok.connect.assert_called_once()


async def test_start_wraps_pyngrok_error_as_tunnel_error() -> None:
    """Underlying PyngrokError propagates as TunnelError with chained cause."""
    pyngrok, ngrok, _conf = _make_fake_pyngrok()
    ngrok.connect.side_effect = _FakePyngrokError("boom")
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)

    with pytest.raises(TunnelError, match="Failed to open ngrok tunnel"):
        await p.start(8000)


async def test_start_wraps_unexpected_exception_as_tunnel_error() -> None:
    """A non-PyngrokError still surfaces as TunnelError, not RuntimeError."""
    pyngrok, ngrok, _conf = _make_fake_pyngrok()
    ngrok.connect.side_effect = RuntimeError("network down")
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)

    with pytest.raises(TunnelError, match="Unexpected error opening ngrok tunnel"):
        await p.start(8000)


async def test_health_disconnected_before_start() -> None:
    pyngrok, _ngrok, _conf = _make_fake_pyngrok()
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)
    h = await p.health()
    assert h.status is TunnelStatus.DISCONNECTED


async def test_health_connected_after_successful_start() -> None:
    pyngrok, _ngrok, _conf = _make_fake_pyngrok("https://abc.ngrok.io")
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)
    await p.start(8000)
    h = await p.health()
    assert h.status is TunnelStatus.CONNECTED
    assert h.public_url == "https://abc.ngrok.io"


async def test_health_error_after_failed_start() -> None:
    """After a failed start(), health() surfaces ERROR with the cached message."""
    pyngrok, ngrok, _conf = _make_fake_pyngrok()
    ngrok.connect.side_effect = _FakePyngrokError("auth bad")
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)

    with pytest.raises(TunnelError):
        await p.start(8000)

    h = await p.health()
    assert h.status is TunnelStatus.ERROR
    assert h.error is not None and "auth bad" in h.error


async def test_stop_calls_disconnect_and_kill() -> None:
    pyngrok, ngrok, _conf = _make_fake_pyngrok("https://abc.ngrok.io")
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)
    await p.start(8000)
    await p.stop()

    ngrok.disconnect.assert_called_once_with("https://abc.ngrok.io")
    ngrok.kill.assert_called_once()
    h = await p.health()
    assert h.status is TunnelStatus.DISCONNECTED


async def test_stop_idempotent() -> None:
    """stop() on a never-started provider is a no-op."""
    pyngrok, ngrok, _conf = _make_fake_pyngrok()
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)
    await p.stop()  # must not raise
    ngrok.disconnect.assert_not_called()
    ngrok.kill.assert_not_called()


async def test_stop_swallows_disconnect_errors() -> None:
    """If disconnect() raises PyngrokError on shutdown, stop() still completes."""
    pyngrok, ngrok, _conf = _make_fake_pyngrok()
    ngrok.disconnect.side_effect = _FakePyngrokError("gone")
    p = NgrokProvider(auth_token="tok", pyngrok_module=pyngrok)
    await p.start(8000)
    await p.stop()  # must not raise
    # kill should still be attempted after disconnect failure
    ngrok.kill.assert_called_once()


def test_empty_auth_token_rejected() -> None:
    """Empty auth_token at construction is a ValueError."""
    with pytest.raises(ValueError, match="non-empty auth_token"):
        NgrokProvider(auth_token="")


async def test_subdomain_flows_through_to_options() -> None:
    """Subdomain kwarg gets passed to ngrok.connect."""
    pyngrok, ngrok, _conf = _make_fake_pyngrok()
    p = NgrokProvider(auth_token="tok", subdomain="myapp", pyngrok_module=pyngrok)
    await p.start(8000)
    assert ngrok.connect.call_args.kwargs["subdomain"] == "myapp"


async def test_region_flows_through_to_conf() -> None:
    """Region kwarg overrides conf.get_default().region."""
    pyngrok, _ngrok, conf = _make_fake_pyngrok()
    p = NgrokProvider(auth_token="tok", region="eu", pyngrok_module=pyngrok)
    await p.start(8000)
    assert conf.get_default().region == "eu"
