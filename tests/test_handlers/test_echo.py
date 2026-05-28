"""Tests for :class:`EchoHandler`."""

from __future__ import annotations

import pytest

from hooky.handlers.echo import EchoHandler


@pytest.mark.asyncio
async def test_echo_returns_body_json_when_present(make_stored) -> None:
    """If ``body_json`` is set, EchoHandler echoes that dict."""
    handler = EchoHandler()
    webhook = make_stored(body_json={"hello": "world"})
    result = await handler.process(webhook)
    assert result == {"webhook_id": webhook.id, "body": {"hello": "world"}}


@pytest.mark.asyncio
async def test_echo_falls_back_to_decoded_text(make_stored) -> None:
    """No JSON dict → decode body as UTF-8."""
    handler = EchoHandler()
    webhook = make_stored(body=b"hello bytes", body_json=None)
    # explicitly drop body_json on the fixture
    webhook = webhook.model_copy(update={"body_json": None})
    result = await handler.process(webhook)
    assert result["body"] == "hello bytes"


@pytest.mark.asyncio
async def test_echo_returns_none_body_for_non_decodable_bytes(make_stored) -> None:
    """Bytes that aren't valid UTF-8 produce ``body=None`` rather than raising."""
    handler = EchoHandler()
    webhook = make_stored(body=b"\xff\xfe\x00bad", body_json=None)
    webhook = webhook.model_copy(update={"body_json": None})
    result = await handler.process(webhook)
    assert result["body"] is None
    assert result["webhook_id"] == webhook.id


def test_echo_handler_name_is_echo() -> None:
    """The handler advertises a stable name."""
    assert EchoHandler().name == "echo"
