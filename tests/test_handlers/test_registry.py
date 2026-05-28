"""Tests for config-driven handler loading (dotted import paths)."""

from __future__ import annotations

import pytest

from hooky.config.schema import HandlerConfig
from hooky.handlers.echo import EchoHandler
from hooky.handlers.registry import (
    HandlerConfigError,
    _resolve_secret_kwargs,
    build_handler,
    build_handler_manager,
)


def test_build_handler_colon_form() -> None:
    """Canonical 'module:Class' path resolves + constructs."""
    cfg = HandlerConfig(path="hooky.handlers.echo:EchoHandler")
    handler = build_handler(cfg)
    assert isinstance(handler, EchoHandler)


def test_build_handler_dotted_form() -> None:
    """Last-segment 'module.Class' path also resolves."""
    cfg = HandlerConfig(path="hooky.handlers.echo.EchoHandler")
    handler = build_handler(cfg)
    assert isinstance(handler, EchoHandler)


def test_build_handler_unimportable_module() -> None:
    cfg = HandlerConfig(path="nonexistent.module:Thing")
    with pytest.raises(HandlerConfigError, match="Cannot import module"):
        build_handler(cfg)


def test_build_handler_missing_attribute() -> None:
    cfg = HandlerConfig(path="hooky.handlers.echo:NotAClass")
    with pytest.raises(HandlerConfigError, match="has no attribute"):
        build_handler(cfg)


def test_build_handler_not_a_webhook_handler() -> None:
    """Resolving to a non-WebhookHandler is a config error."""
    cfg = HandlerConfig(path="hooky.handlers.manager:HandlerManager")
    with pytest.raises(HandlerConfigError, match="not a WebhookHandler subclass"):
        build_handler(cfg)


def test_build_handler_malformed_path() -> None:
    cfg = HandlerConfig(path="noseparators")
    with pytest.raises(HandlerConfigError, match="not a valid dotted path"):
        build_handler(cfg)


def test_build_handler_passes_config_kwargs() -> None:
    """Constructor kwargs from config reach the handler __init__."""
    # LoggerHandler takes an optional `level` kwarg; verify it's accepted.
    cfg = HandlerConfig(path="hooky.handlers.logger:LoggerHandler", config={})
    handler = build_handler(cfg)
    assert handler.name  # constructed without error


def test_build_handler_bad_kwargs_surface_as_config_error() -> None:
    """A constructor that rejects the kwargs yields a clear config error."""
    cfg = HandlerConfig(
        path="hooky.handlers.echo:EchoHandler",
        config={"nonexistent_kwarg": 123},
    )
    with pytest.raises(HandlerConfigError, match="Failed to construct handler"):
        build_handler(cfg)


def test_build_handler_manager_registers_in_order() -> None:
    entries = [
        HandlerConfig(path="hooky.handlers.echo:EchoHandler"),
        HandlerConfig(path="hooky.handlers.logger:LoggerHandler"),
    ]
    manager = build_handler_manager(entries)
    names = [h.name for h in manager.handlers]
    assert names == ["echo", "logger"]


def test_build_handler_manager_empty_list() -> None:
    manager = build_handler_manager([])
    assert manager.handlers == []


def test_build_handler_manager_duplicate_names_is_config_error() -> None:
    """Two entries resolving to the same handler name surface as config error."""
    entries = [
        HandlerConfig(path="hooky.handlers.echo:EchoHandler"),
        HandlerConfig(path="hooky.handlers.echo:EchoHandler"),
    ]
    with pytest.raises(HandlerConfigError, match="Cannot register handler"):
        build_handler_manager(entries)


# ----------------------------------------------------------------------------
# secret_env indirection — secrets reach the handler constructor from env vars
# without ever appearing in config.yaml (mirrors validators' secret_env).
# ----------------------------------------------------------------------------


def test_resolve_secret_kwargs_injects_env_value_as_kwarg() -> None:
    """A secret_env mapping reads the named env var and lands as that kwarg,
    merged alongside plain config kwargs."""
    cfg = HandlerConfig(
        path="myapp.handlers:SmsHandler",
        config={"from_number": "+15551234567"},
        secret_env={"api_token": "MYAPP_API_TOKEN"},
    )
    kwargs = _resolve_secret_kwargs(cfg, env={"MYAPP_API_TOKEN": "tok-abc123"})
    assert kwargs == {"from_number": "+15551234567", "api_token": "tok-abc123"}


def test_resolve_secret_kwargs_empty_is_just_config() -> None:
    """No secret_env -> kwargs are exactly the config dict (a copy)."""
    cfg = HandlerConfig(path="x:Y", config={"a": 1})
    kwargs = _resolve_secret_kwargs(cfg, env={})
    assert kwargs == {"a": 1}
    assert kwargs is not cfg.config  # defensive copy, not the model's dict


def test_resolve_secret_kwargs_missing_env_var_fails_loud() -> None:
    """An unset env var named in secret_env is a config error naming both the
    kwarg and the env var — caught at startup, not mid-dispatch."""
    cfg = HandlerConfig(path="x:Y", secret_env={"api_token": "MYAPP_API_TOKEN"})
    with pytest.raises(HandlerConfigError, match="MYAPP_API_TOKEN.*not set"):
        _resolve_secret_kwargs(cfg, env={})


def test_resolve_secret_kwargs_collision_is_config_error() -> None:
    """The same kwarg in both config and secret_env is ambiguous -> error."""
    cfg = HandlerConfig(
        path="x:Y",
        config={"api_token": "plaintext-oops"},
        secret_env={"api_token": "MYAPP_API_TOKEN"},
    )
    with pytest.raises(HandlerConfigError, match="both"):
        _resolve_secret_kwargs(cfg, env={"MYAPP_API_TOKEN": "tok"})


def test_build_handler_missing_secret_env_fails_before_construction() -> None:
    """build_handler surfaces an unset secret_env var as a config error
    before it ever calls the constructor."""
    cfg = HandlerConfig(
        path="hooky.handlers.echo:EchoHandler",
        secret_env={"api_token": "UNSET_VAR_XYZ"},
    )
    with pytest.raises(HandlerConfigError, match="UNSET_VAR_XYZ.*not set"):
        build_handler(cfg, env={})


def test_build_handler_construction_error_does_not_leak_secret_value() -> None:
    """If construction fails after secrets are resolved, the secret VALUE must
    not appear in the error (EchoHandler takes no kwargs, so this always fails
    — proving both that the resolved kwarg reached the constructor and that
    its value is redacted from the message)."""
    secret = "super-secret-token-do-not-leak"
    cfg = HandlerConfig(
        path="hooky.handlers.echo:EchoHandler",
        secret_env={"api_token": "LEAK_TEST_VAR"},
    )
    with pytest.raises(HandlerConfigError) as exc_info:
        build_handler(cfg, env={"LEAK_TEST_VAR": secret})
    assert secret not in str(exc_info.value)


def test_build_handler_manager_threads_env_to_entries() -> None:
    """build_handler_manager passes env down so secret_env resolves per entry."""
    entries = [
        HandlerConfig(
            path="hooky.handlers.echo:EchoHandler",
            secret_env={"tok": "MISSING_VAR"},
        )
    ]
    with pytest.raises(HandlerConfigError, match="MISSING_VAR.*not set"):
        build_handler_manager(entries, env={})
