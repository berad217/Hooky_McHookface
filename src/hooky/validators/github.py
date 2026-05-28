"""GitHub webhook signature validator.

GitHub sends signatures in ``X-Hub-Signature-256`` formatted as
``sha256=<hex>``. The legacy ``X-Hub-Signature`` (SHA1) header is also
accepted when the SHA256 header is missing, for older integrations that
haven't been updated.

Reference:
    https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
"""

from __future__ import annotations

from collections.abc import Mapping

from hooky.validators.base import (
    SignatureValidator,
    _get_header,
    constant_time_compare,
    hmac_sha1,
    hmac_sha256,
)

_HEADER_SHA256 = "X-Hub-Signature-256"
_HEADER_SHA1 = "X-Hub-Signature"


class GitHubValidator(SignatureValidator):
    """Validator for GitHub webhook deliveries.

    Args:
        secret: GitHub webhook secret (the value configured in the repo /
            org webhook settings).

    Raises:
        ValueError: If ``secret`` is empty.
    """

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("GitHubValidator requires a non-empty secret.")
        self._secret = secret

    def validate(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Validate a GitHub webhook signature.

        Prefers ``X-Hub-Signature-256`` (HMAC-SHA256). Falls back to
        ``X-Hub-Signature`` (HMAC-SHA1) only if the SHA256 header is
        absent — SHA1 is legacy and should not be relied on for new
        integrations.

        Args:
            body: Raw request body.
            headers: Request headers (case-insensitive lookup).

        Returns:
            ``True`` iff the present signature matches the body under the
            configured secret.
        """
        sha256_sig = _get_header(headers, _HEADER_SHA256)
        if sha256_sig:
            expected = f"sha256={hmac_sha256(body, self._secret)}"
            return constant_time_compare(expected, sha256_sig)

        sha1_sig = _get_header(headers, _HEADER_SHA1)
        if sha1_sig:
            expected = f"sha1={hmac_sha1(body, self._secret)}"
            return constant_time_compare(expected, sha1_sig)

        return False
