"""Signature validators package.

Public surface:

- :class:`SignatureValidator` — the ABC validator authors subclass.
- :class:`GenericHMACValidator`, :class:`GitHubValidator`, :class:`StripeValidator`
  — the validators shipped in v1.
- :func:`build_validator`, :func:`build_validator_map`, :class:`ValidatorConfigError`
  — config-driven instantiation used by the app factory.
"""

from hooky.validators.base import SignatureValidator
from hooky.validators.generic import GenericHMACValidator
from hooky.validators.github import GitHubValidator
from hooky.validators.quo import QuoValidator
from hooky.validators.registry import (
    ValidatorConfigError,
    build_validator,
    build_validator_map,
)
from hooky.validators.secret_token import SecretTokenValidator
from hooky.validators.stripe import StripeValidator

__all__ = [
    "GenericHMACValidator",
    "GitHubValidator",
    "QuoValidator",
    "SecretTokenValidator",
    "SignatureValidator",
    "StripeValidator",
    "ValidatorConfigError",
    "build_validator",
    "build_validator_map",
]
