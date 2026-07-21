"""Unit tests for the security primitives (docs/code-plan.md sections 5 and 9).

Pure unit level: passwords, opaque tokens, TOTP and CSRF derivation are exercised
directly -- no HTTP, no app, no database. TOTP codes are computed with pyotp against
the real clock; ``verify_totp`` accepts one 30-second step of drift (valid_window=1),
which also keeps step-boundary races out of these tests.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from mypanel.errors import WeakPasswordError
from mypanel.security import (
    DUMMY_HASH,
    derive_csrf_token,
    generate_token,
    generate_totp_secret,
    hash_password,
    hash_token,
    tokens_match,
    validate_password_strength,
    verify_csrf_token,
    verify_password,
    verify_totp,
)

SAMPLE_PASSWORD = "correct-horse-battery-9!"

HEX64 = re.compile(r"[0-9a-f]{64}")
URLSAFE = re.compile(r"[A-Za-z0-9_-]+")

# Fullwidth digits (U+FF10..U+FF19, as escapes to appease RUF001): "123456" and
# "1234567890" -- str.isdigit() is True for them but str.isascii() is False.
FULLWIDTH_123456 = "\uff11\uff12\uff13\uff14\uff15\uff16"
FULLWIDTH_DIGITS_10 = "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10"


@pytest.fixture(scope="module")
def sample_hash() -> str:
    """One shared Argon2 hash of SAMPLE_PASSWORD (hashing is deliberately slow)."""
    return hash_password(SAMPLE_PASSWORD)


class TestPasswordHashing:
    def test_hash_verify_round_trip(self, sample_hash: str) -> None:
        assert sample_hash != SAMPLE_PASSWORD
        assert sample_hash.startswith("$argon2")
        assert verify_password(SAMPLE_PASSWORD, sample_hash) is True

    def test_hashing_is_salted(self, sample_hash: str) -> None:
        second = hash_password(SAMPLE_PASSWORD)
        assert second != sample_hash
        assert verify_password(SAMPLE_PASSWORD, second) is True

    def test_wrong_password_rejected(self, sample_hash: str) -> None:
        assert verify_password("wrong-horse-battery-9!", sample_hash) is False

    @pytest.mark.parametrize(
        "corrupted",
        [
            pytest.param("", id="empty"),
            pytest.param("not-a-hash", id="no-structure"),
            pytest.param("$argon2id$garbage", id="argon2-prefix-garbage"),
            pytest.param("$2b$12$abcdefghijklmnopqrstuv", id="foreign-scheme"),
        ],
    )
    def test_corrupted_hash_verifies_false(self, corrupted: str) -> None:
        assert verify_password(SAMPLE_PASSWORD, corrupted) is False

    def test_dummy_hash_is_wellformed_and_never_matches(self) -> None:
        # The user-not-found login branch burns one Argon2 pass on DUMMY_HASH so both
        # branches cost the same; it must verify cleanly (no exception) to False.
        assert DUMMY_HASH.startswith("$argon2")
        assert verify_password("any-candidate-password-1", DUMMY_HASH) is False


WEAK_PASSWORDS = [
    pytest.param("", id="empty"),
    pytest.param("Aa1!Aa1!Aa1", id="11-chars-despite-mixed-classes"),
    pytest.param("abcdefghijkl", id="12-lowercase-only"),
    pytest.param("ABCDEFGHIJKL", id="12-uppercase-only"),
    pytest.param("123456789012", id="12-digits-only"),
    pytest.param("!!!!!!!!!!!!", id="12-punctuation-only"),
    pytest.param("密码安全密码安全密码安全", id="12-cjk-single-other-class"),
    pytest.param(FULLWIDTH_DIGITS_10 + FULLWIDTH_123456[:2], id="12-fullwidth-digits-only"),
]

GOOD_PASSWORDS = [
    pytest.param("abcdefghijk1", id="12-lower-plus-digit"),
    pytest.param("abcdefghijkL", id="12-lower-plus-upper"),
    pytest.param("ABCDEFGHIJ-K", id="12-upper-plus-punct"),
    pytest.param(SAMPLE_PASSWORD, id="passphrase"),
    pytest.param("密码安全密码安全密码ab", id="cjk-other-class-plus-lower"),
    pytest.param(FULLWIDTH_DIGITS_10 + "ab", id="fullwidth-digit-class-plus-lower"),
]


class TestPasswordStrength:
    @pytest.mark.parametrize("password", WEAK_PASSWORDS)
    def test_weak_passwords_rejected(self, password: str) -> None:
        with pytest.raises(WeakPasswordError):
            validate_password_strength(password)

    @pytest.mark.parametrize("password", GOOD_PASSWORDS)
    def test_acceptable_passwords_pass(self, password: str) -> None:
        validate_password_strength(password)  # must not raise

    def test_weak_password_error_metadata(self) -> None:
        with pytest.raises(WeakPasswordError) as exc_info:
            validate_password_strength("short")
        assert exc_info.value.status == 422
        assert exc_info.value.code == "auth.weak_password"


class TestTokens:
    def test_generate_token_length_and_charset(self) -> None:
        token = generate_token()
        # 32 random bytes as urlsafe base64 without padding: 43 chars of [A-Za-z0-9_-].
        assert len(token) == 43
        assert URLSAFE.fullmatch(token)

    def test_generate_token_is_random(self) -> None:
        assert generate_token() != generate_token()

    def test_hash_token_stable_sha256_hex(self) -> None:
        token = "session-token-value"
        digest = hash_token(token)
        assert digest == hash_token(token)
        assert HEX64.fullmatch(digest)
        assert digest == hashlib.sha256(token.encode()).hexdigest()
        assert hash_token("session-token-valu3") != digest

    def test_tokens_match_semantics(self) -> None:
        assert tokens_match("same-token", "same-token") is True
        assert tokens_match("same-token", "diff-token") is False
        assert tokens_match("same-token", "same-token-longer") is False
        # Non-ASCII input must not raise (bytes are compared, not str).
        assert tokens_match("令牌-alpha", "令牌-alpha") is True
        assert tokens_match("令牌-alpha", "令牌-bravo") is False


class TestTotp:
    def test_current_code_accepted(self) -> None:
        secret = generate_totp_secret()
        code = pyotp.TOTP(secret).now()
        assert verify_totp(secret, code) is True

    def test_wrong_code_rejected(self) -> None:
        secret = generate_totp_secret()
        totp = pyotp.TOTP(secret)
        now = datetime.now(tz=UTC)
        # Exclude every code that could be valid during this test (steps -1..+2).
        currently_valid = {totp.at(now + timedelta(seconds=30 * step)) for step in range(-1, 3)}
        wrong = next(f"{i:06d}" for i in range(1_000_000) if f"{i:06d}" not in currently_valid)
        assert verify_totp(secret, wrong) is False

    def test_surrounding_whitespace_is_stripped(self) -> None:
        secret = generate_totp_secret()
        code = pyotp.TOTP(secret).now()
        assert verify_totp(secret, f"  {code}\t") is True

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param("12345", id="5-digits"),
            pytest.param("1234567", id="7-digits"),
            pytest.param("12a456", id="contains-letter"),
            pytest.param("12 456", id="inner-whitespace"),
            pytest.param("", id="empty"),
            pytest.param("      ", id="whitespace-only"),
            pytest.param(FULLWIDTH_123456, id="fullwidth-digits-non-ascii"),
        ],
    )
    def test_malformed_codes_rejected(self, code: str) -> None:
        assert verify_totp(generate_totp_secret(), code) is False

    def test_previous_time_step_code_accepted(self) -> None:
        # valid_window=1: the code of the previous 30-second step still verifies.
        secret = generate_totp_secret()
        previous = pyotp.TOTP(secret).at(datetime.now(tz=UTC) - timedelta(seconds=30))
        assert verify_totp(secret, previous) is True


class TestCsrf:
    SECRET = b"\x01" * 32
    OTHER_SECRET = b"\x02" * 32

    def test_derivation_is_deterministic(self) -> None:
        session_hash = hash_token("some-session-token")
        first = derive_csrf_token(self.SECRET, session_hash)
        assert first == derive_csrf_token(self.SECRET, session_hash)
        assert HEX64.fullmatch(first)

    def test_derivation_depends_on_secret_and_session(self) -> None:
        hash_a = hash_token("session-a")
        hash_b = hash_token("session-b")
        token = derive_csrf_token(self.SECRET, hash_a)
        assert derive_csrf_token(self.OTHER_SECRET, hash_a) != token
        assert derive_csrf_token(self.SECRET, hash_b) != token

    def test_verify_accepts_derived_token_only(self) -> None:
        session_hash = hash_token("some-session-token")
        good = derive_csrf_token(self.SECRET, session_hash)
        assert verify_csrf_token(self.SECRET, session_hash, good) is True

        tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
        assert verify_csrf_token(self.SECRET, session_hash, tampered) is False
        assert verify_csrf_token(self.OTHER_SECRET, session_hash, good) is False
        assert verify_csrf_token(self.SECRET, hash_token("other-session"), good) is False

    def test_verify_rejects_empty_presented_token(self) -> None:
        session_hash = hash_token("some-session-token")
        assert verify_csrf_token(self.SECRET, session_hash, "") is False
