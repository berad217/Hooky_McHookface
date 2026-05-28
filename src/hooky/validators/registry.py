"""Build a path-keyed validator map from configuration.

The route layer consults a ``dict[str, SignatureValidator]`` to decide
whether (and how) to validate a given webhook. This module is the seam
between the YAML-shaped :class:`ValidatorConfig` list and that dict.

Secrets are sourced from environment variables, named via
``ValidatorConfig.secret_env``. A missing/empty env var is a fatal config
error — better to fail loud at app construction than silently accept every
request as "no validator configured".
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from hooky.config.schema import ValidatorConfig
from hooky.validators.base import SignatureValidator
from hooky.validators.generic import GenericHMACValidator
from hooky.validators.github import GitHubValidator
from hooky.validators.quo import QuoValidator
from hooky.validators.secret_token import SecretTokenValidator
from hooky.validators.stripe import StripeValidator


class ValidatorConfigError(ValueError):
    """Raised when validator config cannot be resolved into a working validator.

    Examples: missing env var, missing required field for the chosen
    validator type, unknown validator type.
    """


def build_validator(
    cfg: ValidatorConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> SignatureValidator:
    """Instantiate a :class:`SignatureValidator` from a single config entry.

    Args:
        cfg: Validator configuration entry.
        env: Environment mapping to read ``cfg.secret_env`` from. Defaults
            to ``os.environ``. Tests inject a controlled mapping.

    Returns:
        A configured :class:`SignatureValidator` instance.

    Raises:
        ValidatorConfigError: If the secret env var is missing/empty, the
            validator type is unknown, or required type-specific fields are
            missing.
    """
    source = env if env is not None else os.environ
    secret = source.get(cfg.secret_env, "")
    if not secret:
        raise ValidatorConfigError(
            f"Validator for {cfg.path!r}: env var {cfg.secret_env!r} is unset or empty."
        )

    if cfg.type == "github":
        return GitHubValidator(secret=secret)

    if cfg.type == "stripe":
        tolerance = cfg.tolerance_seconds if cfg.tolerance_seconds is not None else 300
        return StripeValidator(secret=secret, tolerance_seconds=tolerance)

    if cfg.type == "quo":
        # tolerance_seconds is None unless configured → no freshness check
        # (the timestamp's unit isn't confirmed live; HMAC integrity stands).
        return QuoValidator(secret=secret, tolerance_seconds=cfg.tolerance_seconds)

    if cfg.type == "generic_hmac":
        if not cfg.header:
            raise ValidatorConfigError(
                f"Validator for {cfg.path!r}: type 'generic_hmac' requires 'header'."
            )
        return GenericHMACValidator(
            secret=secret,
            header=cfg.header,
            algorithm=cfg.algorithm or "sha256",
            prefix=cfg.prefix or "",
        )

    if cfg.type == "secret_token":
        # `header` is optional: defaults to Telegram's secret-token header.
        if cfg.header:
            return SecretTokenValidator(secret=secret, header=cfg.header)
        return SecretTokenValidator(secret=secret)

    # Should be unreachable thanks to the Literal type in ValidatorConfig,
    # but guard for the future when new types are added.
    raise ValidatorConfigError(
        f"Validator for {cfg.path!r}: unknown type {cfg.type!r}."
    )


def build_validator_map(
    entries: list[ValidatorConfig],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, SignatureValidator]:
    """Build a path -> validator dict from a list of configs.

    Args:
        entries: List of validator configuration entries.
        env: Environment mapping for secret resolution. Defaults to
            ``os.environ``.

    Returns:
        Dict mapping ``cfg.path`` to a configured validator. Empty if
        ``entries`` is empty.

    Raises:
        ValidatorConfigError: As per :func:`build_validator`, or if two
            entries share the same path.
    """
    result: dict[str, SignatureValidator] = {}
    for cfg in entries:
        if cfg.path in result:
            raise ValidatorConfigError(
                f"Duplicate validator entry for path {cfg.path!r}."
            )
        result[cfg.path] = build_validator(cfg, env=env)
    return result
