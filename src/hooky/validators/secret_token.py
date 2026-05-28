"""Shared-secret header-equality validator.

Unlike the HMAC validators (github / stripe / quo / generic_hmac), which sign
the request *body*, this validator checks that a named header carries a fixed
shared secret verbatim. The request body is irrelevant.

The motivating case is **Telegram**: when a bot's webhook is registered with a
``secret_token``, Telegram echoes that exact value in the
``X-Telegram-Bot-Api-Secret-Token`` header on every update. There is no body
signature to verify — confidence that the request came from Telegram rests
entirely on this header matching the secret only Telegram and we know.

Reference:
    https://core.telegram.org/bots/api#setwebhook  (the ``secret_token`` param)

The comparison is constant-time (:func:`constant_time_compare`) — a naive
``==`` here would leak the secret a byte at a time under a timing attack, the
same OWASP-grade footgun the HMAC validators guard against.
"""

from __future__ import annotations

from collections.abc import Mapping

from hooky.validators.base import (
    SignatureValidator,
    _get_header,
    constant_time_compare,
)

#: Telegram's fixed echo header. Used as the default when config names no header.
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


class SecretTokenValidator(SignatureValidator):
    """Validate that a header equals a configured shared secret.

    Args:
        secret: The shared secret. The header value must equal this exactly.
        header: Header name carrying the secret. Defaults to Telegram's
            ``X-Telegram-Bot-Api-Secret-Token``; override for other services
            that use the same echo-the-secret scheme.

    Raises:
        ValueError: If ``secret`` is empty (a validator with no secret would
            accept nothing useful and silently weaken the path).
    """

    def __init__(self, secret: str, header: str = TELEGRAM_SECRET_HEADER) -> None:
        if not secret:
            raise ValueError("SecretTokenValidator requires a non-empty secret.")
        self._secret = secret
        self._header = header

    def validate(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Return ``True`` iff the configured header equals the secret.

        The ``body`` is ignored — this is not a body signature. A missing
        header returns ``False`` (fail-closed), never a match.

        Args:
            body: Raw request body. Unused; present for the validator contract.
            headers: Request headers (case-insensitive lookup).

        Returns:
            ``True`` iff ``headers[self._header]`` is present and equals the
            configured secret under a constant-time comparison.
        """
        presented = _get_header(headers, self._header)
        if presented is None:
            return False
        return constant_time_compare(presented, self._secret)
