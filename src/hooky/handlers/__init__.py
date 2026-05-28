"""Handlers package — public re-exports for handler authors and the route layer.

A handler author imports :class:`WebhookHandler` from this package and
implements :meth:`WebhookHandler.process`. Framework code (routes, replay)
imports :class:`HandlerManager` to register and dispatch.
"""

from hooky.handlers.base import WebhookHandler
from hooky.handlers.echo import EchoHandler
from hooky.handlers.logger import LoggerHandler
from hooky.handlers.manager import HandlerManager

__all__ = [
    "EchoHandler",
    "HandlerManager",
    "LoggerHandler",
    "WebhookHandler",
]
