"""Quo (formerly OpenPhone) webhook signature validator.

Quo signs webhooks with the ``openphone-signature`` header (the rebrand did
NOT rename it), formatted as a semicolon-delimited
``<scheme>;<version>;<timestamp>;<base64-signature>`` — e.g.
``hmac;1;1680000000;Ymxhego...==``. Verification:

- HMAC payload is ``<timestamp> + "." + rawbody`` (the timestamp from field 2,
  a literal dot, then the raw request body bytes — never a re-serialized form).
- The signing **key is the base64-decoded** webhook signing secret.
- The digest is HMAC-SHA256, **base64**-encoded, compared to field 3.

Reference:
    https://support.quo.com/core-concepts/integrations/webhooks

Design notes:
- The scheme/version fields (indices 0, 1) are parsed **positionally** and not
  asserted against fixed literals — Quo's docs don't guarantee them as
  constants, so we rely only on the timestamp (index 2) and signature
  (index 3).
- Replay/freshness checking is **optional and off by default**. The timestamp's
  unit (seconds vs. milliseconds) is not yet confirmed against a live Quo
  webhook, so enforcing a tolerance on a units guess could reject valid
  deliveries. The HMAC alone provides integrity + authenticity regardless; set
  ``tolerance_seconds`` to enable freshness checking once the format is
  confirmed live.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Callable, Mapping

from hooky.validators.base import (
    SignatureValidator,
    _get_header,
    constant_time_compare,
)

_HEADER = "openphone-signature"
_MIN_FIELDS = 4
_TIMESTAMP_INDEX = 2
_SIGNATURE_INDEX = 3


class QuoValidator(SignatureValidator):
    """Validator for Quo/OpenPhone webhook deliveries.

    Args:
        secret: The webhook signing secret, **base64-encoded** (as Quo
            provides it). Decoded to raw key bytes at construction.
        tolerance_seconds: If set, reject signatures whose timestamp differs
            from now by more than this many seconds (replay protection).
            Defaults to ``None`` = no freshness check (see module notes).
        time_source: Callable returning current Unix time. Tests pin this;
            production omits it and gets :func:`time.time`.

    Raises:
        ValueError: If ``secret`` is empty, is not valid base64, decodes to
            empty bytes, or ``tolerance_seconds`` is negative.
    """

    def __init__(
        self,
        secret: str,
        *,
        tolerance_seconds: int | None = None,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        if not secret:
            raise ValueError("QuoValidator requires a non-empty secret.")
        if tolerance_seconds is not None and tolerance_seconds < 0:
            raise ValueError(
                f"tolerance_seconds must be >= 0, got {tolerance_seconds}."
            )
        try:
            self._key = base64.b64decode(secret, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "QuoValidator secret must be base64-encoded (Quo's signing "
                "secret is base64)."
            ) from exc
        if not self._key:
            raise ValueError("QuoValidator secret decoded to empty bytes.")
        self._tolerance = tolerance_seconds
        self._time_source = time_source if time_source is not None else time.time

    def validate(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Validate a Quo webhook signature.

        Args:
            body: Raw request body bytes (never re-serialized).
            headers: Request headers (case-insensitive lookup for
                ``openphone-signature``).

        Returns:
            ``True`` iff the header is well-formed, the (optional) timestamp
            tolerance passes, and the base64 HMAC-SHA256 of
            ``"<timestamp>." + body`` under the decoded key matches field 3.
        """
        raw = _get_header(headers, _HEADER)
        if not raw:
            return False

        fields = raw.split(";")
        if len(fields) < _MIN_FIELDS:
            return False
        timestamp = fields[_TIMESTAMP_INDEX].strip()
        provided_sig = fields[_SIGNATURE_INDEX].strip()
        if not timestamp or not provided_sig:
            return False

        # Optional replay/freshness check (off unless tolerance configured).
        if self._tolerance is not None:
            try:
                ts_int = int(timestamp)
            except ValueError:
                return False
            now = int(self._time_source())
            if abs(now - ts_int) > self._tolerance:
                return False

        signed = f"{timestamp}.".encode() + body
        expected = base64.b64encode(
            hmac.new(self._key, signed, hashlib.sha256).digest()
        ).decode("ascii")
        return constant_time_compare(expected, provided_sig)
