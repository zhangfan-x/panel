"""Stateless CSRF tokens derived per session: HMAC-SHA256(secret_key, session_token_hash)."""

from __future__ import annotations

import hashlib
import hmac


def derive_csrf_token(secret_key: bytes, session_token_hash: str) -> str:
    """Derive the CSRF token (hex) for a session; deterministic, so nothing is stored."""
    return hmac.new(secret_key, session_token_hash.encode(), hashlib.sha256).hexdigest()


def verify_csrf_token(secret_key: bytes, session_token_hash: str, presented: str) -> bool:
    """Constant-time check of a presented CSRF token against the derived value."""
    expected = derive_csrf_token(secret_key, session_token_hash)
    return hmac.compare_digest(expected.encode(), presented.encode())
