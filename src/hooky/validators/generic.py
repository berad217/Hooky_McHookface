"""Generic HMAC signature validator.

The workhorse. Useful for any webhook source that signs the raw body with a
shared HMAC secret and ships the hex digest in a configurable header,
optionally with a prefix (e.g. ``"sha256="``).
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

_SUPPORTED_ALGORITHMS = ("sha256", "sha1")


class GenericHMACValidator(SignatureValidator):
    """Configurable HMAC validator.

    Args:
        secret: Shared HMAC secret. Empty string is rejected — a validator
            with no secret is a misconfiguration, not a "validate every
            request as invalid" mode.
        header: Header name carrying the hex digest. Case-insensitive at
            lookup time.
        algorithm: ``"sha256"`` (default) or ``"sha1"``.
        prefix: Optional prefix the sender includes before the digest
            (e.g. ``"sha256="``). Stripped before comparison; if the
            received header is missing the prefix, validation fails.

    Raises:
        ValueError: If ``secret`` is empty or ``algorithm`` is unsupported.
    """

    def __init__(
        self,
        secret: str,
        header: str = "X-Signature",
        algorithm: str = "sha256",
        prefix: str = "",
    ) -> None:
        if not secret:
            raise ValueError("GenericHMACValidator requires a non-empty secret.")
        algorithm_lc = algorithm.lower()
        if algorithm_lc not in _SUPPORTED_ALGORITHMS:
            raise ValueError(
                f"Unsupported algorithm {algorithm!r}; expected one of "
                f"{_SUPPORTED_ALGORITHMS}."
            )

        self._secret = secret
        self._header = header
        self._algorithm = algorithm_lc
        self._prefix = prefix

    def validate(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Validate the HMAC signature.

        Args:
            body: Raw request body.
            headers: Headers mapping (case-insensitive lookup).

        Returns:
            ``True`` iff the configured header is present and its digest
            (after prefix-stripping) matches HMAC(secret, body).
        """
        provided = _get_header(headers, self._header)
        if provided is None or provided == "":
            return False

        if self._prefix:
            if not provided.startswith(self._prefix):
                return False
            provided = provided[len(self._prefix):]

        if self._algorithm == "sha256":
            expected = hmac_sha256(body, self._secret)
        else:
            expected = hmac_sha1(body, self._secret)

        return constant_time_compare(expected, provided)
