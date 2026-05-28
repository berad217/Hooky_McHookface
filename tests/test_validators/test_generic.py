"""Tests for GenericHMACValidator."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from hooky.validators.generic import GenericHMACValidator


def _sign(secret: str, body: bytes, algorithm: str = "sha256") -> str:
    """Helper: produce a hex HMAC of ``body`` with ``secret``."""
    hasher = hashlib.sha256 if algorithm == "sha256" else hashlib.sha1
    return hmac.new(secret.encode(), body, hasher).hexdigest()


def test_validates_correct_sha256_signature() -> None:
    """A correctly-signed body validates."""
    secret = "topsecret"
    body = b'{"event":"ping"}'
    v = GenericHMACValidator(secret=secret, header="X-Signature")
    assert v.validate(body, {"x-signature": _sign(secret, body)}) is True


def test_rejects_wrong_signature() -> None:
    """A bad signature does not validate."""
    v = GenericHMACValidator(secret="topsecret", header="X-Signature")
    assert v.validate(b'{"event":"ping"}', {"x-signature": "0" * 64}) is False


def test_rejects_missing_header() -> None:
    """No header == invalid (not crash)."""
    v = GenericHMACValidator(secret="topsecret", header="X-Signature")
    assert v.validate(b'{"event":"ping"}', {}) is False


def test_rejects_empty_signature() -> None:
    """Empty-string header value is invalid."""
    v = GenericHMACValidator(secret="topsecret", header="X-Signature")
    assert v.validate(b'{"event":"ping"}', {"x-signature": ""}) is False


def test_case_insensitive_header_lookup() -> None:
    """Headers compare case-insensitively even if caller passes mixed case."""
    secret = "topsecret"
    body = b"x"
    v = GenericHMACValidator(secret=secret, header="X-Quo-Signature")
    # Caller used a different case from the configured name:
    assert v.validate(body, {"X-Quo-Signature": _sign(secret, body)}) is True


def test_strips_configured_prefix() -> None:
    """If prefix is configured and present, it's stripped before comparison."""
    secret = "topsecret"
    body = b"payload"
    v = GenericHMACValidator(secret=secret, header="X-Signature", prefix="sha256=")
    sig = "sha256=" + _sign(secret, body)
    assert v.validate(body, {"x-signature": sig}) is True


def test_rejects_when_prefix_missing() -> None:
    """If prefix is configured but the header lacks it, fail closed."""
    secret = "topsecret"
    body = b"payload"
    v = GenericHMACValidator(secret=secret, header="X-Signature", prefix="sha256=")
    # Correct digest but without the required prefix:
    assert v.validate(body, {"x-signature": _sign(secret, body)}) is False


def test_sha1_algorithm() -> None:
    """SHA1 mode is supported (legacy)."""
    secret = "topsecret"
    body = b"x"
    v = GenericHMACValidator(secret=secret, header="X-Signature", algorithm="sha1")
    assert v.validate(body, {"x-signature": _sign(secret, body, "sha1")}) is True


def test_constructor_rejects_empty_secret() -> None:
    """Empty secret is a misconfiguration, not a no-op validator."""
    with pytest.raises(ValueError, match="non-empty secret"):
        GenericHMACValidator(secret="", header="X-Signature")


def test_constructor_rejects_unsupported_algorithm() -> None:
    """Only sha256 and sha1 are accepted."""
    with pytest.raises(ValueError, match="Unsupported algorithm"):
        GenericHMACValidator(secret="x", header="X-Signature", algorithm="md5")


def test_signature_is_body_specific() -> None:
    """A signature for one body does not validate against another."""
    secret = "topsecret"
    v = GenericHMACValidator(secret=secret, header="X-Signature")
    sig_for_a = _sign(secret, b"a")
    assert v.validate(b"a", {"x-signature": sig_for_a}) is True
    assert v.validate(b"b", {"x-signature": sig_for_a}) is False
