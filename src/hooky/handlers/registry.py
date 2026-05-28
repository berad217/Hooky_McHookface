"""Load handlers from configuration by dotted import path.

This is the seam that lets a consuming application register its handlers
via ``config.yaml`` instead of writing launcher code:

.. code-block:: yaml

    handlers:
      - path: "myapp.handlers:SmsHandler"
        config:
          from_number: "+15551234567"
      - path: "hooky.handlers.echo:EchoHandler"

The CLI and the app factory both call :func:`build_handler_manager` to turn
that list into a ready-to-dispatch :class:`HandlerManager`.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from typing import Any

from hooky.config.schema import HandlerConfig
from hooky.handlers.base import WebhookHandler
from hooky.handlers.manager import HandlerManager
from hooky.storage.base import StorageBackend


class HandlerConfigError(ValueError):
    """Raised when a handler config entry cannot be resolved into a handler.

    Examples: unimportable module, missing attribute, the attribute isn't a
    ``WebhookHandler`` subclass, or the constructor rejects the supplied
    ``config`` kwargs.
    """


def _resolve_dotted_path(path: str) -> Any:
    """Import and return the object named by a dotted path.

    Accepts both ``"pkg.mod:attr"`` (canonical) and ``"pkg.mod.attr"``
    (last segment is the attribute). The colon form is unambiguous when the
    attribute itself is nested or the module name is deep; the dotted form
    is convenience.

    Args:
        path: Dotted import path.

    Returns:
        The resolved object (expected to be a class).

    Raises:
        HandlerConfigError: If the module can't be imported or the attribute
            doesn't exist.
    """
    if ":" in path:
        module_name, _, attr = path.partition(":")
    else:
        module_name, _, attr = path.rpartition(".")
    if not module_name or not attr:
        raise HandlerConfigError(
            f"Handler path {path!r} is not a valid dotted path "
            "(expected 'package.module:ClassName')."
        )

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise HandlerConfigError(
            f"Cannot import module {module_name!r} for handler {path!r}: {exc}"
        ) from exc

    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise HandlerConfigError(
            f"Module {module_name!r} has no attribute {attr!r} (handler {path!r})."
        ) from exc


def _resolve_secret_kwargs(
    cfg: HandlerConfig, *, env: Mapping[str, str]
) -> dict[str, Any]:
    """Read each ``secret_env`` mapping from the environment into kwargs.

    Args:
        cfg: Handler config entry carrying ``config`` + ``secret_env``.
        env: Environment mapping (``os.environ`` in production; injectable
            for tests).

    Returns:
        The merged kwargs dict: ``cfg.config`` plus one entry per
        ``secret_env`` mapping, with the env var's value.

    Raises:
        HandlerConfigError: If a kwarg appears in both ``config`` and
            ``secret_env`` (ambiguous), or a referenced env var is unset.
    """
    kwargs: dict[str, Any] = dict(cfg.config)
    for kwarg_name, env_var in cfg.secret_env.items():
        if kwarg_name in kwargs:
            raise HandlerConfigError(
                f"Handler {cfg.path!r}: kwarg {kwarg_name!r} is set in both "
                "'config' and 'secret_env' — remove one. (Secrets belong in "
                "'secret_env'.)"
            )
        value = env.get(env_var)
        if value is None:
            raise HandlerConfigError(
                f"Handler {cfg.path!r}: secret_env maps kwarg {kwarg_name!r} to "
                f"environment variable {env_var!r}, which is not set."
            )
        kwargs[kwarg_name] = value
    return kwargs


def build_handler(cfg: HandlerConfig, *, env: Mapping[str, str] | None = None) -> WebhookHandler:
    """Instantiate a single :class:`WebhookHandler` from a config entry.

    Args:
        cfg: Handler config entry (dotted path + optional ``config`` kwargs +
            optional ``secret_env`` env-var indirection).
        env: Environment mapping used to resolve ``secret_env``. Defaults to
            ``os.environ``; tests inject a dict.

    Returns:
        A constructed handler instance.

    Raises:
        HandlerConfigError: If the path doesn't resolve to a
            ``WebhookHandler`` subclass, a ``secret_env`` var is unset,
            a kwarg collides between ``config`` and ``secret_env``, or
            construction fails.
    """
    obj = _resolve_dotted_path(cfg.path)

    if not (isinstance(obj, type) and issubclass(obj, WebhookHandler)):
        raise HandlerConfigError(
            f"Handler {cfg.path!r} resolved to {obj!r}, which is not a "
            "WebhookHandler subclass."
        )

    kwargs = _resolve_secret_kwargs(cfg, env=os.environ if env is None else env)

    try:
        handler = obj(**kwargs)
    except Exception as exc:  # noqa: BLE001 — surface ANY construction error clearly
        # Don't echo kwargs — they may now carry resolved secret values.
        raise HandlerConfigError(
            f"Failed to construct handler {cfg.path!r}: {type(exc).__name__}: {exc}"
        ) from exc

    return handler


def build_handler_manager(
    entries: list[HandlerConfig],
    *,
    storage: StorageBackend | None = None,
    env: Mapping[str, str] | None = None,
) -> HandlerManager:
    """Build a :class:`HandlerManager` from a list of handler configs.

    Args:
        entries: Handler config entries. Empty list yields an empty manager.
        storage: Storage backend the manager records ``handler_runs`` to.
            ``None`` disables persistence (used by some tests).
        env: Environment mapping used to resolve each entry's ``secret_env``.
            Defaults to ``os.environ``; tests inject a dict.

    Returns:
        A :class:`HandlerManager` with every configured handler registered,
        in config order.

    Raises:
        HandlerConfigError: As per :func:`build_handler`, or if two entries
            resolve to handlers with the same ``name`` (the manager rejects
            duplicates — we surface that as a config error with context).
    """
    manager = HandlerManager(storage=storage)
    for cfg in entries:
        handler = build_handler(cfg, env=env)
        try:
            manager.register(handler)
        except ValueError as exc:
            raise HandlerConfigError(
                f"Cannot register handler {cfg.path!r}: {exc}"
            ) from exc
    return manager
