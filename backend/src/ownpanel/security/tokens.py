"""Opaque bearer tokens (sessions, bootstrap): generation, hashing, comparison.

Only the SHA-256 hex digest of a token is ever stored server-side; the plaintext
exists solely in the client's cookie or in the one-time CLI output.
"""

from __future__ import annotations

import hashlib
import secrets

TOKEN_BYTES = 32


def generate_token() -> str:
    """Generate a random 256-bit urlsafe token (the plaintext handed to the client)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token, the only form persisted server-side."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_match(a: str, b: str) -> bool:
    """Constant-time equality check for tokens or token hashes."""
    return secrets.compare_digest(a.encode(), b.encode())
