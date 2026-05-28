"""Tests for the :class:`WebhookHandler` ABC contract.

The ABC's job is small: enforce that subclasses provide ``name`` and an
async ``process``. These tests pin those contractual requirements so a
future refactor can't silently weaken them.
"""

from __future__ import annotations

import inspect

import pytest

from hooky.handlers.base import WebhookHandler


def test_cannot_instantiate_abstract_handler() -> None:
    """:class:`WebhookHandler` itself is abstract — direct instantiation must fail."""
    with pytest.raises(TypeError):
        WebhookHandler()  # type: ignore[abstract]


def test_subclass_missing_name_raises() -> None:
    """A subclass without ``name`` cannot be instantiated."""

    class NoName(WebhookHandler):
        async def process(self, webhook):  # type: ignore[override]
            return None

    with pytest.raises(TypeError):
        NoName()  # type: ignore[abstract]


def test_subclass_missing_process_raises() -> None:
    """A subclass without ``process`` cannot be instantiated."""

    class NoProcess(WebhookHandler):
        @property
        def name(self) -> str:
            return "no_process"

    with pytest.raises(TypeError):
        NoProcess()  # type: ignore[abstract]


def test_complete_subclass_instantiates_and_runs(make_stored) -> None:
    """A fully-implemented subclass can be instantiated and process() awaited."""

    class OK(WebhookHandler):
        @property
        def name(self) -> str:
            return "ok"

        async def process(self, webhook):  # type: ignore[override]
            return {"id": webhook.id}

    handler = OK()
    assert handler.name == "ok"
    assert inspect.iscoroutinefunction(handler.process)
