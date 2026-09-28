"""Argon2id password hashing and password strength policy."""

from __future__ import annotations

import secrets

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from ownpanel.errors import WeakPasswordError

MIN_PASSWORD_LENGTH = 12

_password_hash = PasswordHash.recommended()

# Valid Argon2 hash of a random throwaway password, precomputed at import time.
# The "user not found" login branch verifies against this so both branches spend
# one Argon2 pass and stay indistinguishable by timing.
DUMMY_HASH: str = _password_hash.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    """Hash a password with Argon2id (pwdlib recommended parameters)."""
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored hash; malformed hash strings verify as False."""
    try:
        return _password_hash.verify(password, password_hash)
    except UnknownHashError:
        return False


def _char_class(char: str) -> str:
    if char.islower():
        return "lower"
    if char.isupper():
        return "upper"
    if char.isdigit():
        return "digit"
    return "other"


def validate_password_strength(password: str) -> None:
    """Enforce the password policy: length >= 12 and at least two character classes.

    Raises:
        WeakPasswordError: If the password does not satisfy the policy.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if len({_char_class(char) for char in password}) < 2:
        raise WeakPasswordError(
            "Password must mix at least two character classes "
            "(lowercase, uppercase, digits, other)."
        )
