"""TOTP enrolment and verification (pyotp wrapper)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp


def generate_totp_secret() -> str:
    """Generate a random base32 secret for TOTP enrolment."""
    return pyotp.random_base32()


def otpauth_uri(secret: str, account_name: str, issuer: str = "mypanel") -> str:
    """Build the otpauth:// provisioning URI for authenticator apps."""
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def match_totp_timecode(secret: str, code: str, *, valid_window: int = 1) -> int | None:
    """Return the counter (timecode) a valid code matches, or None if it is invalid.

    One 30-second step of drift is accepted either side (``valid_window=1``). The
    returned integer is the pyotp timecode of the matched step, letting callers
    enforce one-time use: accept a code only when its counter is strictly greater
    than the last one recorded for the device, blocking replay of a captured code
    across login/re-auth within its validity window.

    Anything that is not exactly six ASCII digits (after stripping surrounding
    whitespace) is rejected without touching the secret.
    """
    code = code.strip()
    if len(code) != 6 or not (code.isascii() and code.isdigit()):
        return None
    totp = pyotp.TOTP(secret)
    now = datetime.now(tz=UTC)
    for offset in range(-valid_window, valid_window + 1):
        moment = now + timedelta(seconds=offset * totp.interval)
        if totp.verify(code, for_time=moment, valid_window=0):
            return totp.timecode(moment)
    return None


def verify_totp(secret: str, code: str) -> bool:
    """Check a six-digit TOTP code, accepting one 30-second step of clock drift.

    Anything that is not exactly six ASCII digits (after stripping surrounding
    whitespace) is rejected without touching the secret.
    """
    return match_totp_timecode(secret, code) is not None
