"""Structured logging setup using structlog.

Logs are emitted as JSON to stdout by default. The framework never logs raw
secrets — callers should redact before logging.

This module is intentionally small in Sprint 1. Later sprints may add request-id
context vars, log-level config, and integration with FastAPI's access logs.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog


def configure_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configure structlog + stdlib logging for the framework.

    Idempotent: calling repeatedly with the same arguments has no harmful effect.

    Args:
        level: Log level name (e.g. ``"INFO"``, ``"DEBUG"``).
        json_format: If ``True`` (default), emit JSON lines. If ``False``, emit
            human-readable console output (useful for local dev / tests).

    Raises:
        ValueError: If ``level`` is not a recognised logging level name.
    """
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        raise ValueError(f"Unknown log level: {level!r}")

    # Configure stdlib root logger to forward to stdout.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_format:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        # Caching is a micro-optimisation that bites us in test runs where
        # ``sys.stdout`` gets swapped (pytest ``capsys``) — the cached bound
        # logger holds a now-closed file. The cost of skipping the cache is
        # negligible (one extra dict lookup per ``.info(...)`` call) and the
        # operational simplicity is worth it.
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger.

    Args:
        name: Optional logger name (typically ``__name__``).

    Returns:
        A bound structlog logger ready to emit structured events.
    """
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    return cast("structlog.stdlib.BoundLogger", logger)
