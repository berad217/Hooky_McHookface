"""Tests for SecretTokenValidator."""

from __future__ import annotations

import pytest

from hooky.validators.secret_token import (
    TELEGRAM_SECRET_HEADER,
    SecretTokenValidator,
)

_SECRET = "a-long-unguessable-telegram-secret-token"


def test_validates_matching_header() -> None:
    """The configured header equalling the secret validates."""
    v = SecretTokenValidator(secret=_SECRET)
    headers = {TELEGRAM_SECRET_HEADER.lower(): _SECRET}
    assert v.validate(b"{}", headers) is True


def test_rejects_wrong_value() -> None:
    """A header present but not equal to the secret fails closed."""
    v = SecretTokenValidator(secret=_SECRET)
    headers = {TELEGRAM_SECRET_HEADER.lower(): "not-the-secret"}
    assert v.validate(b"{}", headers) is False


def test_rejects_missing_header() -> None:
    """No header == invalid (not a crash, not a pass)."""
    v = SecretTokenValidator(secret=_SECRET)
    assert v.validate(b"{}", {}) is False


def test_rejects_empty_header_value() -> None:
    """Empty-string header value never matches a non-empty secret."""
    v = SecretTokenValidator(secret=_SECRET)
    headers = {TELEGRAM_SECRET_HEADER.lower(): ""}
    assert v.validate(b"{}", headers) is False


def test_case_insensitive_header_lookup() -> None:
    """Header name compares case-insensitively even if caller uses mixed case."""
    v = SecretTokenValidator(secret=_SECRET)
    # Telegram's real (mixed-case) header, not the route-lowercased form:
    assert v.validate(b"{}", {TELEGRAM_SECRET_HEADER: _SECRET}) is True


def test_body_is_ignored() -> None:
    """Validation depends only on the header, never the body (no body signature)."""
    v = SecretTokenValidator(secret=_SECRET)
    headers = {TELEGRAM_SECRET_HEADER.lower(): _SECRET}
    assert v.validate(b"any body at all", headers) is True
    assert v.validate(b"", headers) is True


def test_custom_header() -> None:
    """A non-Telegram header name is honoured when configured."""
    v = SecretTokenValidator(secret=_SECRET, header="X-My-Secret")
    assert v.validate(b"{}", {"x-my-secret": _SECRET}) is True
    # The default Telegram header should not be consulted in that case:
    assert v.validate(b"{}", {TELEGRAM_SECRET_HEADER.lower(): _SECRET}) is False


def test_default_header_is_telegrams() -> None:
    """The default header is Telegram's secret-token header."""
    assert TELEGRAM_SECRET_HEADER == "X-Telegram-Bot-Api-Secret-Token"


def test_constructor_rejects_empty_secret() -> None:
    """Empty secret is a misconfiguration, not a no-op validator."""
    with pytest.raises(ValueError, match="non-empty secret"):
        SecretTokenValidator(secret="")
