"""Tests for the TunnelProvider ABC + TunnelHealth model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hooky.tunnel.base import TunnelHealth, TunnelProvider, TunnelStatus


def test_tunnel_provider_is_abstract() -> None:
    """Instantiating the ABC directly must raise."""
    with pytest.raises(TypeError):
        TunnelProvider()  # type: ignore[abstract]


def test_subclass_missing_methods_cannot_instantiate() -> None:
    """A subclass that fails to implement any abstract method is also abstract."""

    class IncompleteOne(TunnelProvider):  # type: ignore[misc]
        async def start(self, port: int) -> str:  # type: ignore[override]
            return "x"

    with pytest.raises(TypeError):
        IncompleteOne()  # type: ignore[abstract]


def test_tunnel_health_model_roundtrips() -> None:
    """TunnelHealth is a plain pydantic model with the fields docs say."""
    now = datetime.now(UTC)
    h = TunnelHealth(
        status=TunnelStatus.CONNECTED,
        public_url="https://example.com",
        last_checked=now,
        error=None,
    )
    assert h.status is TunnelStatus.CONNECTED
    assert h.public_url == "https://example.com"
    assert h.last_checked == now
    assert h.error is None


def test_tunnel_status_values() -> None:
    """The three statuses we promised in the docs are present and string-valued."""
    assert TunnelStatus.DISCONNECTED.value == "disconnected"
    assert TunnelStatus.CONNECTED.value == "connected"
    assert TunnelStatus.ERROR.value == "error"
