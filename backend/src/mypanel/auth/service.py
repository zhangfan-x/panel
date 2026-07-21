"""Authentication service: bootstrap, login state machine, sessions, TOTP, re-auth.

All public mutating methods commit their own successful transaction and raise
(before any mutation) on every failure path, so a caller catching an ``AppError``
can safely commit an independent audit row on the same session afterwards.

SQLite returns naive UTC datetimes; every stored timestamp is passed through
``_aware()`` before comparison with ``datetime.now(tz=UTC)``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mypanel.config import Settings
from mypanel.db.models import MfaDevice, Session, SessionState, Setting, User
from mypanel.errors import (
    BootstrapDoneError,
    BootstrapError,
    InvalidCredentialsError,
    InvalidTotpError,
    NotFoundError,
    ReauthRequiredError,
    SessionStateError,
    ValidationFailedError,
)
from mypanel.security import (
    DUMMY_HASH,
    derive_csrf_token,
    generate_token,
    generate_totp_secret,
    hash_password,
    hash_token,
    match_totp_timecode,
    otpauth_uri,
    tokens_match,
    validate_password_strength,
    verify_password,
    verify_totp,
)

BOOTSTRAP_TOKEN_HASH_KEY = "bootstrap_token_hash"
BOOTSTRAP_TOKEN_EXPIRES_AT_KEY = "bootstrap_token_expires_at"
BOOTSTRAP_TOKEN_TTL = timedelta(minutes=15)

# The owner is the single account the whole system assumes exists at most once.
# Pinning its primary key makes "exactly one owner" a database invariant: two
# bootstrap requests that race past the owner_exists() pre-check both try to
# INSERT this id and, because SQLite serialises writers, the loser hits a
# primary-key conflict instead of quietly creating a second owner.
_OWNER_USER_ID = 1

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")

_TOTP_KIND = "totp"
_LAST_SEEN_REFRESH = timedelta(seconds=60)


def _aware(dt: datetime) -> datetime:
    """Reattach UTC to naive datetimes read back from SQLite."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class AuthService:
    """All authentication flows over one request-scoped ``AsyncSession``."""

    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def owner_exists(self) -> bool:
        """True once the (single) owner account has been created."""
        return await self._db.scalar(select(User.id).limit(1)) is not None

    async def issue_bootstrap_token(self) -> str:
        """Create a fresh 15-minute bootstrap token and return its plaintext.

        Only the SHA-256 hash and the expiry are stored (``settings`` table);
        re-issuing overwrites any previous token.

        Raises:
            BootstrapDoneError: If the owner account already exists.
        """
        if await self.owner_exists():
            raise BootstrapDoneError()
        token = generate_token()
        now = datetime.now(tz=UTC)
        await self._put_setting(BOOTSTRAP_TOKEN_HASH_KEY, hash_token(token), now)
        await self._put_setting(
            BOOTSTRAP_TOKEN_EXPIRES_AT_KEY, (now + BOOTSTRAP_TOKEN_TTL).isoformat(), now
        )
        await self._db.commit()
        return token

    async def create_owner(
        self, token: str, username: str, password: str, *, ip: str, user_agent: str
    ) -> tuple[User, str]:
        """Create the owner account and its ``totp_enroll`` session.

        The bootstrap token is checked first (constant-time against the stored
        hash) so callers without a valid token learn nothing else. On success
        the token record is destroyed.

        Returns:
            The new user and the plaintext session token (for the cookie).

        Raises:
            BootstrapError: Unknown, mismatching, expired or corrupt token.
            BootstrapDoneError: If the owner account already exists.
            ValidationFailedError: If the username violates ``USERNAME_RE``.
            WeakPasswordError: If the password fails the strength policy.
        """
        stored_hash = await self._get_setting(BOOTSTRAP_TOKEN_HASH_KEY)
        if stored_hash is None or not tokens_match(hash_token(token), stored_hash):
            raise BootstrapError("Bootstrap token is invalid.")
        expires_raw = await self._get_setting(BOOTSTRAP_TOKEN_EXPIRES_AT_KEY)
        if expires_raw is None:
            raise BootstrapError("Bootstrap token is invalid.")
        try:
            expires_at = _aware(datetime.fromisoformat(expires_raw))
        except ValueError:
            raise BootstrapError("Bootstrap token record is corrupt.") from None
        now = datetime.now(tz=UTC)
        if now > expires_at:
            raise BootstrapError("Bootstrap token has expired.")
        if await self.owner_exists():
            raise BootstrapDoneError()
        if USERNAME_RE.fullmatch(username) is None:
            raise ValidationFailedError(
                "Username must be 3-32 characters of letters, digits, '_', '.' or '-'."
            )
        validate_password_strength(password)
        user = User(
            id=_OWNER_USER_ID,
            username=username,
            password_hash=hash_password(password),
            created_at=now,
            password_changed_at=now,
        )
        self._db.add(user)
        # owner_exists() above is only advisory: two concurrent bootstraps can both
        # pass it. Flushing the owner now forces the second one to collide on the
        # fixed users primary key (SQLite serialises writers); we map that to the same
        # "already done" outcome rather than letting a second owner slip through or a
        # raw IntegrityError bubble up as a 500. Flushing first also inserts the owner
        # ahead of its session row, whose user_id is a literal (no ORM relationship to
        # order the writes).
        try:
            await self._db.flush()
        except IntegrityError:
            await self._db.rollback()
            raise BootstrapDoneError() from None
        session, raw_token = self._new_session(
            _OWNER_USER_ID, SessionState.TOTP_ENROLL, ip=ip, user_agent=user_agent
        )
        self._db.add(session)
        await self._delete_setting(BOOTSTRAP_TOKEN_HASH_KEY)
        await self._delete_setting(BOOTSTRAP_TOKEN_EXPIRES_AT_KEY)
        await self._db.commit()
        return user, raw_token

    async def login(
        self, username: str, password: str, *, ip: str, user_agent: str
    ) -> tuple[Session, str]:
        """Verify the password and create the post-password session.

        Unknown usernames still cost one Argon2 verification (against
        ``DUMMY_HASH``) and raise the same error as a wrong password. The new
        session is ``mfa_pending`` when a confirmed TOTP device exists,
        otherwise ``totp_enroll`` (recovery path after ``reset-owner``).

        Returns:
            The new session and its plaintext token (for the cookie).

        Raises:
            InvalidCredentialsError: Unknown username or wrong password.
        """
        user = await self._db.scalar(select(User).where(User.username == username))
        if user is None:
            verify_password(password, DUMMY_HASH)
            raise InvalidCredentialsError()
        if not verify_password(password, user.password_hash):
            raise InvalidCredentialsError()
        state = (
            SessionState.MFA_PENDING
            if await self._confirmed_device(user.id) is not None
            else SessionState.TOTP_ENROLL
        )
        session, raw_token = self._new_session(user.id, state, ip=ip, user_agent=user_agent)
        self._db.add(session)
        await self._db.commit()
        return session, raw_token

    async def get_valid_session(self, raw_token: str) -> tuple[Session, User] | None:
        """Resolve a cookie token to a live (not revoked, not expired) session.

        Refreshes ``last_seen_at`` at most once per 60 seconds to limit writes.
        Returns None for unknown, revoked, idle-expired or absolutely expired
        tokens.
        """
        session = await self._db.scalar(
            select(Session).where(Session.token_hash == hash_token(raw_token))
        )
        if session is None or session.revoked_at is not None:
            return None
        now = datetime.now(tz=UTC)
        if self._is_expired(session, now):
            return None
        user = await self._db.get(User, session.user_id)
        if user is None:
            return None
        if now - _aware(session.last_seen_at) > _LAST_SEEN_REFRESH:
            session.last_seen_at = now
            await self._db.commit()
        return session, user

    async def verify_totp_login(self, session: Session, code: str) -> None:
        """Complete a login: check the code against the confirmed device, go active.

        The matched code is spent (one-time use), so it cannot be replayed to a
        second login or to re-auth within its validity window.

        Raises:
            SessionStateError: If the session is not ``mfa_pending``.
            InvalidTotpError: Wrong, already-spent code (or no confirmed device left).
        """
        if session.state != SessionState.MFA_PENDING:
            raise SessionStateError("TOTP verification requires an mfa_pending session.")
        device = await self._confirmed_device(session.user_id)
        timecode = match_totp_timecode(device.secret, code) if device is not None else None
        if device is None or timecode is None or not self._consume_totp(device, timecode):
            raise InvalidTotpError()
        session.state = SessionState.ACTIVE.value
        await self._db.commit()

    async def totp_setup(self, session: Session, user: User) -> tuple[str, str]:
        """Start (or restart) TOTP enrolment: create/replace the pending device.

        Allowed for ``totp_enroll`` sessions and for ``active`` sessions with a
        recent re-authentication (device rebind).

        Returns:
            The base32 secret and the otpauth:// provisioning URI.

        Raises:
            ReauthRequiredError: Active session without recent re-auth.
            SessionStateError: Any other non-enrollable state (``mfa_pending``).
        """
        self._ensure_enroll_allowed(session, require_reauth=True)
        secret = generate_totp_secret()
        now = datetime.now(tz=UTC)
        device = await self._pending_device(user.id)
        if device is None:
            self._db.add(
                MfaDevice(
                    user_id=user.id,
                    kind=_TOTP_KIND,
                    secret=secret,
                    confirmed_at=None,
                    created_at=now,
                )
            )
        else:
            device.secret = secret
            device.created_at = now
            device.last_timecode = None  # fresh secret: no code has been spent yet
        await self._db.commit()
        return secret, otpauth_uri(secret, user.username)

    async def totp_confirm(self, session: Session, user: User, code: str) -> None:
        """Confirm the pending device; drop all other devices for the user.

        Recent re-auth is not re-checked here: `totp_setup` already gated the
        creation of the pending device, and the confirm step legitimately
        happens after the 60-second window (user scans the QR code first).
        ``mfa_pending`` sessions are still rejected. A ``totp_enroll`` session
        is upgraded to ``active``.

        Raises:
            SessionStateError: If the session is ``mfa_pending``.
            NotFoundError: No pending (unconfirmed) device for this user.
            InvalidTotpError: Wrong code.
        """
        self._ensure_enroll_allowed(session, require_reauth=False)
        device = await self._pending_device(user.id)
        if device is None:
            raise NotFoundError("No pending TOTP enrollment for this user.")
        if not verify_totp(device.secret, code):
            raise InvalidTotpError()
        device.confirmed_at = datetime.now(tz=UTC)
        await self._db.execute(
            delete(MfaDevice).where(MfaDevice.user_id == user.id, MfaDevice.id != device.id)
        )
        if session.state == SessionState.TOTP_ENROLL:
            session.state = SessionState.ACTIVE.value
        await self._db.commit()

    async def reauth(self, session: Session, user: User, password: str, code: str) -> None:
        """Re-verify password + TOTP for an active session; stamp ``reauth_at``.

        The matched TOTP code is spent (one-time use): a code already used to log in
        cannot be replayed here, breaking the single-captured-code takeover chain.

        Raises:
            SessionStateError: If the session is not ``active``.
            InvalidCredentialsError: Wrong password.
            InvalidTotpError: Wrong, already-spent code (or no confirmed device).
        """
        if session.state != SessionState.ACTIVE:
            raise SessionStateError("Re-authentication requires an active session.")
        if not verify_password(password, user.password_hash):
            raise InvalidCredentialsError()
        device = await self._confirmed_device(user.id)
        timecode = match_totp_timecode(device.secret, code) if device is not None else None
        if device is None or timecode is None or not self._consume_totp(device, timecode):
            raise InvalidTotpError()
        session.reauth_at = datetime.now(tz=UTC)
        await self._db.commit()

    def has_recent_reauth(self, session: Session) -> bool:
        """True while ``reauth_at`` is within ``limits.reauth_window_seconds``."""
        if session.reauth_at is None:
            return False
        window = timedelta(seconds=self._settings.limits.reauth_window_seconds)
        return datetime.now(tz=UTC) - _aware(session.reauth_at) <= window

    async def logout(self, session: Session) -> None:
        """Revoke the current session."""
        session.revoked_at = datetime.now(tz=UTC)
        await self._db.commit()

    async def list_sessions(self, user: User, current_token_hash: str) -> list[dict[str, Any]]:
        """List the user's live sessions (not revoked, not expired), newest first.

        Each item: ``id``, ``created_at``/``last_seen_at`` (UTC ISO strings),
        ``ip``, ``user_agent`` and ``current`` (whether it matches
        `current_token_hash`).
        """
        now = datetime.now(tz=UTC)
        result = await self._db.scalars(
            select(Session)
            .where(Session.user_id == user.id, Session.revoked_at.is_(None))
            .order_by(Session.created_at.desc())
        )
        return [
            {
                "id": s.id,
                "created_at": _aware(s.created_at).isoformat(),
                "last_seen_at": _aware(s.last_seen_at).isoformat(),
                "ip": s.ip,
                "user_agent": s.user_agent,
                "current": tokens_match(s.token_hash, current_token_hash),
            }
            for s in result
            if not self._is_expired(s, now)
        ]

    async def revoke_session(self, user: User, session_id: int) -> None:
        """Revoke one of the user's sessions by id (idempotent).

        Raises:
            NotFoundError: Unknown id or a session belonging to another user.
        """
        target = await self._db.get(Session, session_id)
        if target is None or target.user_id != user.id:
            raise NotFoundError("Session not found.")
        if target.revoked_at is None:
            target.revoked_at = datetime.now(tz=UTC)
            await self._db.commit()

    async def change_password(
        self, user: User, session: Session, current_password: str, new_password: str
    ) -> None:
        """Change the password and revoke all of the user's other sessions.

        Raises:
            InvalidCredentialsError: Wrong current password.
            WeakPasswordError: New password fails the strength policy.
        """
        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentialsError("Current password is incorrect.")
        validate_password_strength(new_password)
        now = datetime.now(tz=UTC)
        user.password_hash = hash_password(new_password)
        user.password_changed_at = now
        await self._db.execute(
            update(Session)
            .where(
                Session.user_id == user.id,
                Session.id != session.id,
                Session.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await self._db.commit()

    def csrf_token_for(self, session: Session) -> str:
        """Stateless CSRF token for this session (HMAC of its token hash)."""
        return derive_csrf_token(self._settings.secret_key, session.token_hash)

    def _new_session(
        self, user_id: int, state: SessionState, *, ip: str, user_agent: str
    ) -> tuple[Session, str]:
        """Build a not-yet-added Session row plus its plaintext token."""
        raw_token = generate_token()
        now = datetime.now(tz=UTC)
        session = Session(
            token_hash=hash_token(raw_token),
            user_id=user_id,
            state=state.value,
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + timedelta(hours=self._settings.limits.session_max_hours),
            reauth_at=None,
            revoked_at=None,
            ip=ip,
            user_agent=user_agent,
        )
        return session, raw_token

    def _is_expired(self, session: Session, now: datetime) -> bool:
        idle = timedelta(minutes=self._settings.limits.session_idle_minutes)
        return (
            now > _aware(session.absolute_expires_at)
            or now - _aware(session.last_seen_at) > idle
        )

    def _ensure_enroll_allowed(self, session: Session, *, require_reauth: bool) -> None:
        if session.state == SessionState.TOTP_ENROLL:
            return
        if session.state == SessionState.ACTIVE:
            if require_reauth and not self.has_recent_reauth(session):
                raise ReauthRequiredError("Rebinding TOTP requires recent re-authentication.")
            return
        raise SessionStateError("TOTP enrollment is not permitted in this session state.")

    def _consume_totp(self, device: MfaDevice, timecode: int) -> bool:
        """Enforce one-time use of an already-matched TOTP code.

        Accept the code only if its counter is newer than the last one recorded for
        the device, then record it. This blocks replay of a single captured code
        across the login and re-auth steps within its ~90-second validity window
        (RFC 6238 section 5.2). Returns False when the code has already been spent.
        """
        if device.last_timecode is not None and timecode <= device.last_timecode:
            return False
        device.last_timecode = timecode
        return True

    async def _confirmed_device(self, user_id: int) -> MfaDevice | None:
        # Bound to a local first: mypy resolves the scalar() overload to Any when
        # the call is checked directly against this return type.
        device: MfaDevice | None = await self._db.scalar(
            select(MfaDevice).where(
                MfaDevice.user_id == user_id,
                MfaDevice.kind == _TOTP_KIND,
                MfaDevice.confirmed_at.is_not(None),
            )
        )
        return device

    async def _pending_device(self, user_id: int) -> MfaDevice | None:
        device: MfaDevice | None = await self._db.scalar(
            select(MfaDevice).where(
                MfaDevice.user_id == user_id,
                MfaDevice.kind == _TOTP_KIND,
                MfaDevice.confirmed_at.is_(None),
            )
        )
        return device

    async def _get_setting(self, key: str) -> str | None:
        row = await self._db.get(Setting, key)
        if row is None:
            return None
        value = json.loads(row.value)
        return value if isinstance(value, str) else None

    async def _put_setting(self, key: str, value: str, now: datetime) -> None:
        await self._db.merge(Setting(key=key, value=json.dumps(value), updated_at=now))

    async def _delete_setting(self, key: str) -> None:
        await self._db.execute(delete(Setting).where(Setting.key == key))
