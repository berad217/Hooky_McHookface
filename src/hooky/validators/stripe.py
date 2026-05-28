"""Stripe webhook signature validator.

Stripe sends signatures in ``Stripe-Signature`` formatted as
``t=<timestamp>,v1=<sig>[,v1=<sig>...]``. The HMAC payload is
``<timestamp>.<body>`` and the secret is the ``whsec_...`` value from the
Stripe dashboard.

A timestamp tolerance (default 300s) protects against replay: signatures
older than the tolerance are rejected even if the HMAC is otherwise valid.

Reference:
    https://stripe.com/docs/webhooks/signatures
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from hooky.validators.base import (
    SignatureValidator,
    _get_header,
    constant_time_compare,
    hmac_sha256,
)

_HEADER = "Stripe-Signature"
_DEFAULT_TOLERANCE_SECONDS = 300


class StripeValidator(SignatureValidator):
    """Validator for Stripe webhook deliveries.

    Args:
        secret: Stripe webhook signing secret (``whsec_...``).
        tolerance_seconds: Maximum allowed absolute difference between the
            timestamp in the header and current time. Defaults to 300s
            (Stripe's recommended value).
        time_source: Callable returning current Unix time. Tests override
            this to pin "now" against the timestamp in fixture headers;
            production callers omit it and get :func:`time.time`.

    Raises:
        ValueError: If ``secret`` is empty or ``tolerance_seconds`` is
            negative.
    """

    def __init__(
        self,
        secret: str,
        tolerance_seconds: int = _DEFAULT_TOLERANCE_SECONDS,
        *,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        if not secret:
            raise ValueError("StripeValidator requires a non-empty secret.")
        if tolerance_seconds < 0:
            raise ValueError(
                f"tolerance_seconds must be >= 0, got {tolerance_seconds}."
            )
        self._secret = secret
        self._tolerance = tolerance_seconds
        self._time_source = time_source if time_source is not None else time.time

    def validate(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Validate a Stripe webhook signature.

        Args:
            body: Raw request body.
            headers: Request headers (case-insensitive lookup for
                ``Stripe-Signature``).

        Returns:
            ``True`` iff the header is well-formed, the timestamp is within
            tolerance, and at least one ``v1=`` signature matches
            HMAC-SHA256(``"<timestamp>.<body>"``) under the secret.
        """
        raw = _get_header(headers, _HEADER)
        if not raw:
            return False

        timestamp_str, v1_sigs = _parse_signature_header(raw)
        if timestamp_str is None or not v1_sigs:
            return False

        try:
            timestamp_int = int(timestamp_str)
        except ValueError:
            return False

        now = int(self._time_source())
        if abs(now - timestamp_int) > self._tolerance:
            return False

        signed = f"{timestamp_str}.".encode() + body
        expected = hmac_sha256(signed, self._secret)

        for sig in v1_sigs:
            if constant_time_compare(expected, sig):
                return True
        return False


def _parse_signature_header(header: str) -> tuple[str | None, list[str]]:
    """Parse a Stripe-Signature header into (timestamp, [v1 sigs]).

    The header format is ``t=<ts>,v1=<sig>[,v1=<sig>...]`` with optional
    ``v0=`` (legacy, ignored). Unknown keys are skipped. Items missing
    ``=`` are skipped.

    Args:
        header: Raw header value.

    Returns:
        Tuple of (timestamp string or ``None``, list of v1 signature
        strings — possibly empty).
    """
    timestamp: str | None = None
    v1_sigs: list[str] = []
    for item in header.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "t":
            timestamp = value
        elif key == "v1":
            v1_sigs.append(value)
    return timestamp, v1_sigs
