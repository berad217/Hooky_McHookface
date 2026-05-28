"""Tests for the validator registry (config -> instances)."""

from __future__ import annotations

import pytest

from hooky.config.schema import ValidatorConfig
from hooky.validators.generic import GenericHMACValidator
from hooky.validators.github import GitHubValidator
from hooky.validators.registry import (
    ValidatorConfigError,
    build_validator,
    build_validator_map,
)
from hooky.validators.secret_token import (
    TELEGRAM_SECRET_HEADER,
    SecretTokenValidator,
)
from hooky.validators.stripe import StripeValidator


def test_build_github_validator() -> None:
    cfg = ValidatorConfig(path="/webhook/gh", type="github", secret_env="GH_SECRET")
    v = build_validator(cfg, env={"GH_SECRET": "abc"})
    assert isinstance(v, GitHubValidator)


def test_build_stripe_validator_default_tolerance() -> None:
    cfg = ValidatorConfig(path="/webhook/s", type="stripe", secret_env="S")
    v = build_validator(cfg, env={"S": "whsec_x"})
    assert isinstance(v, StripeValidator)


def test_build_stripe_validator_custom_tolerance() -> None:
    cfg = ValidatorConfig(
        path="/webhook/s", type="stripe", secret_env="S", tolerance_seconds=60
    )
    v = build_validator(cfg, env={"S": "whsec_x"})
    # Reach into internals: a unit test verifying config plumbing, not the public surface.
    assert isinstance(v, StripeValidator)
    assert v._tolerance == 60  # type: ignore[attr-defined]


def test_build_generic_hmac_validator() -> None:
    cfg = ValidatorConfig(
        path="/webhook/q",
        type="generic_hmac",
        secret_env="QSECRET",
        header="X-Quo-Signature",
        algorithm="sha256",
        prefix="",
    )
    v = build_validator(cfg, env={"QSECRET": "topsecret"})
    assert isinstance(v, GenericHMACValidator)


def test_generic_hmac_requires_header() -> None:
    """generic_hmac without a header config is a malformed entry."""
    cfg = ValidatorConfig(path="/webhook/q", type="generic_hmac", secret_env="QSECRET")
    with pytest.raises(ValidatorConfigError, match="requires 'header'"):
        build_validator(cfg, env={"QSECRET": "topsecret"})


def test_build_secret_token_validator_default_header() -> None:
    """secret_token with no header config defaults to Telegram's header."""
    cfg = ValidatorConfig(
        path="/webhook/telegram", type="secret_token", secret_env="TG"
    )
    v = build_validator(cfg, env={"TG": "tok"})
    assert isinstance(v, SecretTokenValidator)
    assert v._header == TELEGRAM_SECRET_HEADER  # type: ignore[attr-defined]


def test_build_secret_token_validator_custom_header() -> None:
    """A configured header overrides the Telegram default."""
    cfg = ValidatorConfig(
        path="/webhook/x", type="secret_token", secret_env="TG", header="X-My-Secret"
    )
    v = build_validator(cfg, env={"TG": "tok"})
    assert isinstance(v, SecretTokenValidator)
    assert v._header == "X-My-Secret"  # type: ignore[attr-defined]


def test_missing_env_var_is_config_error() -> None:
    """An empty/missing secret env var fails loudly at construction."""
    cfg = ValidatorConfig(path="/webhook/gh", type="github", secret_env="MISSING")
    with pytest.raises(ValidatorConfigError, match="MISSING"):
        build_validator(cfg, env={})


def test_empty_env_var_value_is_config_error() -> None:
    """Empty string is treated as unset."""
    cfg = ValidatorConfig(path="/webhook/gh", type="github", secret_env="EMPTY")
    with pytest.raises(ValidatorConfigError, match="unset or empty"):
        build_validator(cfg, env={"EMPTY": ""})


def test_build_validator_map_keys_on_path() -> None:
    """The map's keys are the configured paths, exact match."""
    cfgs = [
        ValidatorConfig(path="/webhook/gh", type="github", secret_env="GH"),
        ValidatorConfig(path="/webhook/s", type="stripe", secret_env="S"),
    ]
    m = build_validator_map(cfgs, env={"GH": "a", "S": "b"})
    assert set(m.keys()) == {"/webhook/gh", "/webhook/s"}
    assert isinstance(m["/webhook/gh"], GitHubValidator)
    assert isinstance(m["/webhook/s"], StripeValidator)


def test_build_validator_map_rejects_duplicate_paths() -> None:
    """Two configs with the same path is a config error, not silent overwrite."""
    cfgs = [
        ValidatorConfig(path="/webhook/gh", type="github", secret_env="GH"),
        ValidatorConfig(path="/webhook/gh", type="github", secret_env="GH2"),
    ]
    with pytest.raises(ValidatorConfigError, match="Duplicate"):
        build_validator_map(cfgs, env={"GH": "a", "GH2": "b"})


def test_build_validator_map_empty() -> None:
    """Empty list -> empty map. No env needed."""
    assert build_validator_map([], env={}) == {}
