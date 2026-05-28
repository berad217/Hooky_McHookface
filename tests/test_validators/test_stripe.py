"""Tests for StripeValidator.

Stripe's signature format: ``t=<unix-ts>,v1=<hex>[,v1=<hex>...]``.
The signed payload is ``"<unix-ts>." + body``. The validator also checks
that the timestamp is within ``tolerance_seconds`` of "now".

We inject ``time_source`` to pin "now" for deterministic tests.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from hooky.validators.stripe import StripeValidator

# Pinned "now" for all tests — matches the ``t=`` in the fixture headers.
_NOW = 1_700_000_000
_SECRET = "whsec_test_abc123"


def _stripe_sig(secret: str, timestamp: int, body: bytes) -> str:
    """Produce a v1 signature for a (timestamp, body) under ``secret``."""
    signed = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def _make_validator(tolerance: int = 300) -> StripeValidator:
    """Validator with ``time_source`` pinned at ``_NOW``."""
    return StripeValidator(secret=_SECRET, tolerance_seconds=tolerance, time_source=lambda: _NOW)


def test_validates_correct_signature_at_now() -> None:
    """Body signed at current time with single v1 validates."""
    body = b'{"event": "charge.succeeded"}'
    sig = _stripe_sig(_SECRET, _NOW, body)
    header = f"t={_NOW},v1={sig}"
    assert _make_validator().validate(body, {"stripe-signature": header}) is True


def test_rejects_wrong_signature() -> None:
    """Bad v1 -> invalid."""
    body = b'{"x": 1}'
    header = f"t={_NOW},v1={'0' * 64}"
    assert _make_validator().validate(body, {"stripe-signature": header}) is False


def test_rejects_missing_header() -> None:
    """No Stripe-Signature header -> invalid."""
    assert _make_validator().validate(b"x", {"x-other": "1"}) is False


def test_rejects_header_without_timestamp() -> None:
    """Malformed header with no ``t=`` is invalid."""
    body = b"x"
    sig = _stripe_sig(_SECRET, _NOW, body)
    header = f"v1={sig}"
    assert _make_validator().validate(body, {"stripe-signature": header}) is False


def test_rejects_header_without_v1() -> None:
    """Header with timestamp but no v1 signature is invalid."""
    header = f"t={_NOW}"
    assert _make_validator().validate(b"x", {"stripe-signature": header}) is False


def test_rejects_outside_tolerance() -> None:
    """A signature older than tolerance is rejected, even if HMAC is valid."""
    body = b"x"
    too_old = _NOW - 1000  # well past default 300s tolerance
    sig = _stripe_sig(_SECRET, too_old, body)
    header = f"t={too_old},v1={sig}"
    assert _make_validator(tolerance=300).validate(body, {"stripe-signature": header}) is False


def test_accepts_inside_tolerance() -> None:
    """A signature slightly old but inside tolerance validates."""
    body = b"x"
    a_bit_old = _NOW - 250  # inside default 300s
    sig = _stripe_sig(_SECRET, a_bit_old, body)
    header = f"t={a_bit_old},v1={sig}"
    assert _make_validator(tolerance=300).validate(body, {"stripe-signature": header}) is True


def test_future_timestamp_also_bounded_by_tolerance() -> None:
    """Clock skew the other way (future ts) also rejected past tolerance."""
    body = b"x"
    in_future = _NOW + 1000
    sig = _stripe_sig(_SECRET, in_future, body)
    header = f"t={in_future},v1={sig}"
    assert _make_validator(tolerance=300).validate(body, {"stripe-signature": header}) is False


def test_multiple_v1_signatures_accepts_if_any_match() -> None:
    """Stripe sometimes ships multiple v1 entries (key rotation). Any match validates."""
    body = b"x"
    good = _stripe_sig(_SECRET, _NOW, body)
    header = f"t={_NOW},v1={'f' * 64},v1={good}"
    assert _make_validator().validate(body, {"stripe-signature": header}) is True


def test_ignores_unknown_scheme_keys() -> None:
    """Stripe legacy ``v0=`` (and any unknown key) is ignored, not crashed on."""
    body = b"x"
    sig = _stripe_sig(_SECRET, _NOW, body)
    header = f"t={_NOW},v0=legacy,v1={sig}"
    assert _make_validator().validate(body, {"stripe-signature": header}) is True


def test_non_integer_timestamp_invalid() -> None:
    """A ``t=`` value that isn't an integer is rejected (not crash)."""
    body = b"x"
    sig = _stripe_sig(_SECRET, _NOW, body)
    header = f"t=not-a-number,v1={sig}"
    assert _make_validator().validate(body, {"stripe-signature": header}) is False


def test_case_insensitive_header_lookup() -> None:
    """Real Stripe sends ``Stripe-Signature``; mixed-case still works."""
    body = b"x"
    sig = _stripe_sig(_SECRET, _NOW, body)
    header = f"t={_NOW},v1={sig}"
    assert _make_validator().validate(body, {"Stripe-Signature": header}) is True


def test_constructor_rejects_empty_secret() -> None:
    """Empty secret is a misconfiguration."""
    with pytest.raises(ValueError, match="non-empty secret"):
        StripeValidator(secret="")


def test_constructor_rejects_negative_tolerance() -> None:
    """Negative tolerance makes no sense."""
    with pytest.raises(ValueError, match="tolerance_seconds"):
        StripeValidator(secret="x", tolerance_seconds=-1)
