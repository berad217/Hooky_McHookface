"""Tests for :class:`LoggerHandler`.

structlog is configured with a ``PrintLoggerFactory`` writing to stdout, so
we capture stdout directly to assert the emission. We assert that an event
key matching ``webhook_logged`` appears at INFO level after the handler
processes a webhook.
"""

from __future__ import annotations

import pytest

from hooky.handlers.logger import LoggerHandler
from hooky.log import configure_logging


@pytest.mark.asyncio
async def test_logger_emits_info_event(
    make_stored, capsys: pytest.CaptureFixture[str]
) -> None:
    """Processing a webhook writes a ``webhook_logged`` event to stdout.

    structlog's :class:`PrintLoggerFactory` captures ``sys.stdout`` at
    ``configure_logging`` call time. ``capsys`` swaps stdout for the duration
    of the test then restores it — meaning the structlog factory holds a
    reference to a closed stream once the test exits. We re-call
    ``configure_logging`` inside this test (binding the captured stream) and
    again in the cleanup so subsequent tests get a fresh stream.
    """
    configure_logging(level="INFO", json_format=True)
    handler = LoggerHandler()
    webhook = make_stored()
    try:
        await handler.process(webhook)
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "webhook_logged" in combined
        assert webhook.id in combined
    finally:
        # Rebind structlog to the post-test real stdout, otherwise downstream
        # tests crash with "I/O operation on closed file".
        import structlog

        structlog.reset_defaults()
        configure_logging(level="INFO", json_format=True)


@pytest.mark.asyncio
async def test_logger_returns_ack_dict(make_stored) -> None:
    """Logger returns a small dict that persists cleanly."""
    handler = LoggerHandler()
    webhook = make_stored()
    result = await handler.process(webhook)
    assert result == {"logged": True, "webhook_id": webhook.id}


def test_logger_handler_name_is_logger() -> None:
    """The handler advertises a stable name."""
    assert LoggerHandler().name == "logger"
