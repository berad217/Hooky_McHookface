"""Signature validator abstract base class.

The :class:`SignatureValidator` ABC is the public contract for validator
authors. Implementations are intentionally narrow:

- One synchronous method, ``validate(body: bytes, headers: Mapping[str, str])
  -> bool``. Validators compute fast — pure HMAC arithmetic — so async would
  be ceremony with no payoff. The route awaits ``storage.update_signature_status``,
  not the validator itself.
- Secrets and configuration live on the validator instance (passed via
  ``__init__``). The route does not handle secrets directly; it just calls
  ``validate``.
- Header lookups inside concrete validators are case-insensitive. The route
  pre-lowercases header keys (see ``routes._normalise_headers``), so the
  common path is already cheap, but validators stay defensive in case a
  caller wires them up directly.

This module also exposes ``hmac_sha256`` / ``hmac_sha1`` / ``constant_time_compare``
helpers so concrete validators don't each re-import :mod:`hmac` /
:mod:`hashlib`. These are the same primitives the old ``webhook-reciever``
codebase used; lifted near-verbatim per SPEC §Sprint 3 lifting note.
"""

from __future__ import annotations

import hashlib
import hmac
from abc import ABC, abstractmethod
from collections.abc import Mapping


class SignatureValidator(ABC):
    """Abstract base class for webhook signature validators.

    Concrete validators implement :meth:`validate`. The method receives the
    raw request body and a (lowercased) header mapping; it returns ``True``
    iff the signature is valid for the secret the validator was configured
    with.

    Validators MUST use constant-time comparison
    (:func:`constant_time_compare` below) for the final equality check —
    timing leaks here are an OWASP-grade bug.
    """

    @abstractmethod
    def validate(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Return ``True`` if ``body`` carries a valid signature.

        Args:
            body: Raw request body as bytes. Validators MUST sign the raw
                bytes, never a re-encoded JSON form — re-encoding changes
                whitespace and breaks signatures.
            headers: Header mapping. Implementations should look up by
                lowercase key (the route pre-lowercases), but should still
                handle mixed-case input gracefully if called directly.

        Returns:
            ``True`` if the signature in ``headers`` matches the body under
            this validator's secret. ``False`` for any other reason: missing
            header, malformed header, timestamp out of tolerance, or
            mismatched HMAC.
        """


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup.

    Args:
        headers: Header mapping. May have lowercase keys (route-provided) or
            mixed-case keys (direct caller).
        name: Header name to look up; comparison is case-insensitive.

    Returns:
        The first matching header value, or ``None`` if not present.
    """
    target = name.lower()
    # Fast path: the route normalises keys to lowercase before passing in.
    if target in headers:
        return headers[target]
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def hmac_sha256(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 hex digest.

    Args:
        body: The bytes to sign.
        secret: Shared secret.

    Returns:
        Lowercase hex-encoded digest.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def hmac_sha1(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA1 hex digest.

    Args:
        body: The bytes to sign.
        secret: Shared secret.

    Returns:
        Lowercase hex-encoded digest.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string compare to prevent signature timing leaks.

    Args:
        a: First string.
        b: Second string.

    Returns:
        ``True`` iff the byte encodings are equal.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
