"""Tests for the SignatureValidator ABC + shared helpers."""

from __future__ import annotations

import pytest

from hooky.validators.base import (
    SignatureValidator,
    _get_header,
    constant_time_compare,
    hmac_sha1,
    hmac_sha256,
)


def test_signature_validator_is_abstract() -> None:
    """Instantiating the ABC directly must raise."""
    with pytest.raises(TypeError):
        SignatureValidator()  # type: ignore[abstract]


def test_subclass_missing_validate_cannot_instantiate() -> None:
    """A subclass that fails to implement ``validate`` is also abstract."""

    class Incomplete(SignatureValidator):  # type: ignore[misc]
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_get_header_lowercase_path() -> None:
    """The fast path: caller already lowercased keys."""
    assert _get_header({"x-foo": "bar"}, "X-Foo") == "bar"


def test_get_header_mixed_case_fallback() -> None:
    """If the dict has mixed-case keys, case-insensitive match still works."""
    assert _get_header({"X-Hub-Signature-256": "abc"}, "x-hub-signature-256") == "abc"


def test_get_header_missing_returns_none() -> None:
    """Missing header returns ``None``, not KeyError."""
    assert _get_header({"x-other": "1"}, "x-foo") is None


def test_hmac_sha256_matches_known_vector() -> None:
    """HMAC-SHA256 against a published test vector."""
    # https://datatracker.ietf.org/doc/html/rfc4231 - RFC test case 1
    expected = "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
    assert hmac_sha256(b"Hi There", "\x0b" * 20) == expected


def test_hmac_sha1_matches_known_vector() -> None:
    """HMAC-SHA1 against a published test vector."""
    # https://datatracker.ietf.org/doc/html/rfc2202 - RFC test case 1
    expected = "b617318655057264e28bc0b6fb378c8ef146be00"
    assert hmac_sha1(b"Hi There", "\x0b" * 20) == expected


def test_constant_time_compare_equal_strings() -> None:
    """Equal strings compare True."""
    assert constant_time_compare("hello", "hello") is True


def test_constant_time_compare_unequal_strings() -> None:
    """Unequal strings compare False (regardless of length)."""
    assert constant_time_compare("hello", "world") is False
    assert constant_time_compare("hello", "helloo") is False
    assert constant_time_compare("", "x") is False
