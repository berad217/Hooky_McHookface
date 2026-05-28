"""Tests for the Quo (OpenPhone) webhook signature validator."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from hooky.validators.quo import QuoValidator

# A valid base64-encoded signing secret (as Quo provides it).
SECRET = base64.b64encode(b"super-secret-signing-key").decode("ascii")
BODY = b'{"type":"message.received","data":{"object":{"body":"hi"}}}'
HEADER = "openphone-signature"
TS = "1680000000"


def _sign(
    secret_b64: str,
    timestamp: str,
    body: bytes,
    *,
    scheme: str = "hmac",
    version: str = "1",
) -> str:
    """Build a valid openphone-signature header value for the given inputs."""
    key = base64.b64decode(secret_b64)
    digest = hmac.new(key, f"{timestamp}.".encode() + body, hashlib.sha256).digest()
    sig = base64.b64encode(digest).decode("ascii")
    return f"{scheme};{version};{timestamp};{sig}"


def test_valid_signature_passes() -> None:
    v = QuoValidator(SECRET)
    header = _sign(SECRET, TS, BODY)
    assert v.validate(BODY, {HEADER: header}) is True


def test_missing_header_returns_false() -> None:
    assert QuoValidator(SECRET).validate(BODY, {}) is False


def test_case_insensitive_header_lookup() -> None:
    v = QuoValidator(SECRET)
    assert v.validate(BODY, {"OpenPhone-Signature": _sign(SECRET, TS, BODY)}) is True


def test_too_few_fields_returns_false() -> None:
    # Missing the signature field entirely.
    assert QuoValidator(SECRET).validate(BODY, {HEADER: f"hmac;1;{TS}"}) is False


def test_wrong_signature_returns_false() -> None:
    bad = f"hmac;1;{TS};{base64.b64encode(b'nope').decode()}"
    assert QuoValidator(SECRET).validate(BODY, {HEADER: bad}) is False


def test_tampered_body_returns_false() -> None:
    header = _sign(SECRET, TS, BODY)
    assert QuoValidator(SECRET).validate(BODY + b"x", {HEADER: header}) is False


def test_wrong_secret_returns_false() -> None:
    other = base64.b64encode(b"a-different-key").decode("ascii")
    header = _sign(other, TS, BODY)
    assert QuoValidator(SECRET).validate(BODY, {HEADER: header}) is False


def test_scheme_and_version_tokens_not_asserted() -> None:
    # Parsing is positional; non-'hmac'/'1' leading tokens must still validate
    # as long as timestamp (field 2) and signature (field 3) are correct.
    header = _sign(SECRET, TS, BODY, scheme="xyz", version="9")
    assert QuoValidator(SECRET).validate(BODY, {HEADER: header}) is True


def test_empty_secret_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        QuoValidator("")


def test_non_base64_secret_raises() -> None:
    with pytest.raises(ValueError, match="base64"):
        QuoValidator("%%%not-base64%%%")


def test_negative_tolerance_raises() -> None:
    with pytest.raises(ValueError, match="tolerance_seconds"):
        QuoValidator(SECRET, tolerance_seconds=-1)


def test_no_tolerance_accepts_old_timestamp() -> None:
    # Default (no tolerance): an ancient timestamp still passes on HMAC alone.
    header = _sign(SECRET, "100", BODY)
    assert QuoValidator(SECRET).validate(BODY, {HEADER: header}) is True


def test_tolerance_rejects_stale_timestamp() -> None:
    v = QuoValidator(SECRET, tolerance_seconds=300, time_source=lambda: 1680000000.0)
    header = _sign(SECRET, "1679990000", BODY)  # ~2.8h old
    assert v.validate(BODY, {HEADER: header}) is False


def test_tolerance_accepts_fresh_timestamp() -> None:
    v = QuoValidator(SECRET, tolerance_seconds=300, time_source=lambda: 1680000100.0)
    header = _sign(SECRET, TS, BODY)  # 100s old, within 300s
    assert v.validate(BODY, {HEADER: header}) is True


def test_tolerance_with_noninteger_timestamp_returns_false() -> None:
    v = QuoValidator(SECRET, tolerance_seconds=300, time_source=lambda: 1680000000.0)
    # Build a header with a non-integer timestamp but otherwise-valid HMAC.
    header = _sign(SECRET, "not-a-number", BODY)
    assert v.validate(BODY, {HEADER: header}) is False
