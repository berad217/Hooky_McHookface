"""Tests for GitHubValidator."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from hooky.validators.github import GitHubValidator


def _gh_sig256(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _gh_sig1(secret: str, body: bytes) -> str:
    return "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


# Real-shape GitHub fixture: small JSON body + GitHub's documented signing.
# Body matches the example payload GitHub uses in their docs.
_FIXTURE_SECRET = "It's a Secret to Everybody"
_FIXTURE_BODY = b"Hello, World!"
# Pre-computed via openssl: matches `https://docs.github.com/en/webhooks/.../validating`
_FIXTURE_SIG_256 = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
_FIXTURE_SIG_1 = _gh_sig1(_FIXTURE_SECRET, _FIXTURE_BODY)


def test_github_docs_known_vector_sha256() -> None:
    """The exact example from GitHub's webhook-signing docs validates."""
    v = GitHubValidator(secret=_FIXTURE_SECRET)
    assert v.validate(_FIXTURE_BODY, {"x-hub-signature-256": _FIXTURE_SIG_256}) is True


def test_github_docs_known_vector_sha1_fallback() -> None:
    """If only SHA1 header is present, legacy fallback works."""
    v = GitHubValidator(secret=_FIXTURE_SECRET)
    assert v.validate(_FIXTURE_BODY, {"x-hub-signature": _FIXTURE_SIG_1}) is True


def test_sha256_preferred_over_sha1() -> None:
    """When both headers are present, SHA256 wins. (Verified by giving sha1 a bad value.)"""
    v = GitHubValidator(secret=_FIXTURE_SECRET)
    headers = {
        "x-hub-signature-256": _FIXTURE_SIG_256,
        "x-hub-signature": "sha1=deadbeef",
    }
    assert v.validate(_FIXTURE_BODY, headers) is True


def test_rejects_wrong_sha256() -> None:
    """Bad SHA256 fails (and does NOT silently fall through to SHA1)."""
    v = GitHubValidator(secret=_FIXTURE_SECRET)
    headers = {
        "x-hub-signature-256": "sha256=" + "0" * 64,
        # Even with a valid SHA1, the SHA256-present-but-wrong case must fail:
        "x-hub-signature": _FIXTURE_SIG_1,
    }
    assert v.validate(_FIXTURE_BODY, headers) is False


def test_rejects_missing_both_headers() -> None:
    """No GitHub headers -> invalid."""
    v = GitHubValidator(secret=_FIXTURE_SECRET)
    assert v.validate(_FIXTURE_BODY, {"x-other": "x"}) is False


def test_rejects_wrong_secret() -> None:
    """Right algorithm + body but wrong secret -> invalid."""
    v = GitHubValidator(secret="wrong-secret")
    assert v.validate(_FIXTURE_BODY, {"x-hub-signature-256": _FIXTURE_SIG_256}) is False


def test_case_insensitive_header_lookup() -> None:
    """Real GitHub sends ``X-Hub-Signature-256``; we lowercase, but mixed-case still works."""
    v = GitHubValidator(secret=_FIXTURE_SECRET)
    assert v.validate(_FIXTURE_BODY, {"X-Hub-Signature-256": _FIXTURE_SIG_256}) is True


def test_constructor_rejects_empty_secret() -> None:
    """Empty secret -> ValueError."""
    with pytest.raises(ValueError, match="non-empty secret"):
        GitHubValidator(secret="")


def test_signature_with_unknown_prefix_is_invalid() -> None:
    """A signature with neither sha256= nor sha1= prefix is invalid."""
    v = GitHubValidator(secret=_FIXTURE_SECRET)
    # We have to use SHA1 fallback header — SHA256 header isn't present, so
    # the SHA1 branch will reject because there's no `sha1=` prefix either.
    raw_no_prefix = _FIXTURE_SIG_1.split("=", 1)[1]  # strip "sha1=" prefix
    assert v.validate(_FIXTURE_BODY, {"x-hub-signature": raw_no_prefix}) is False
