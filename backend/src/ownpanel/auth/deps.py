"""FastAPI dependencies: request-scoped DB session, auth guards, CSRF, re-auth.

App state contract (provided by the app factory): ``app.state.sessionmaker``
(``async_sessionmaker[AsyncSession]``), ``app.state.settings`` (``Settings``)
and ``app.state.limiters`` (``dict[str, SlidingWindowLimiter]`` with keys
``login_ip``/``login_user``/``reauth``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownpanel.auth.service import AuthService
from ownpanel.config import Settings
from ownpanel.db.models import Session, SessionState, User
from ownpanel.errors import (
    AuthRequiredError,
    CsrfError,
    ReauthRequiredError,
    SessionStateError,
)
from ownpanel.security import SlidingWindowLimiter, verify_csrf_token

COOKIE_NAME = "ownpanel_session"

CSRF_HEADER = "X-CSRF-Token"


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session; closed (uncommitted work rolled back) on exit."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as db:
        yield db


def get_settings_from_app(request: Request) -> Settings:
    """Settings instance attached to the application by the app factory."""
    settings: Settings = request.app.state.settings
    return settings


def get_limiters(request: Request) -> dict[str, SlidingWindowLimiter]:
    """Shared rate limiters attached to the application by the app factory."""
    limiters: dict[str, SlidingWindowLimiter] = request.app.state.limiters
    return limiters


def get_auth_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> AuthService:
    """AuthService bound to the request's DB session."""
    return AuthService(db, settings)


@dataclass
class CurrentAuth:
    """The authenticated caller: DB session row, user row and the raw cookie token."""

    session: Session
    user: User
    raw_token: str


async def require_session(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> CurrentAuth:
    """Require a valid session cookie in any state.

    Raises:
        AuthRequiredError: Missing cookie or unknown/revoked/expired token.
    """
    raw_token = request.cookies.get(COOKIE_NAME)
    if raw_token is None:
        raise AuthRequiredError()
    found = await service.get_valid_session(raw_token)
    if found is None:
        raise AuthRequiredError()
    session, user = found
    return CurrentAuth(session=session, user=user, raw_token=raw_token)


async def require_active_session(
    auth: Annotated[CurrentAuth, Depends(require_session)],
) -> CurrentAuth:
    """Require a fully authenticated (``active``) session.

    Raises:
        SessionStateError: Session is in any other state.
    """
    if auth.session.state != SessionState.ACTIVE:
        raise SessionStateError("This endpoint requires a fully authenticated session.")
    return auth


async def require_csrf(
    request: Request,
    auth: Annotated[CurrentAuth, Depends(require_session)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> None:
    """Require a valid ``X-CSRF-Token`` header for the current session.

    Raises:
        CsrfError: Missing or mismatching header (constant-time comparison).
    """
    presented = request.headers.get(CSRF_HEADER)
    if presented is None or not verify_csrf_token(
        settings.secret_key, auth.session.token_hash, presented
    ):
        raise CsrfError()


async def require_recent_reauth(
    auth: Annotated[CurrentAuth, Depends(require_session)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    """Require a re-authentication within the configured window.

    Raises:
        ReauthRequiredError: ``reauth_at`` unset or outside the window.
    """
    if not service.has_recent_reauth(auth.session):
        raise ReauthRequiredError()


# Reusable Annotated dependency aliases for feature routers (import from here).
DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings_from_app)]
LimitersDep = Annotated[dict[str, SlidingWindowLimiter], Depends(get_limiters)]
SessionDep = Annotated[CurrentAuth, Depends(require_session)]
ActiveSessionDep = Annotated[CurrentAuth, Depends(require_active_session)]


async def resolve_stream_auth(request: Request) -> CurrentAuth:
    """Validate the session cookie for a long-lived stream (SSE/WebSocket).

    Streaming endpoints must NOT depend on ``get_db``/``require_active_session``:
    those hold a DB session open for the whole response, which for an
    indefinitely long stream leaks a connection. This opens its own short-lived
    session, validates, closes it, and returns the (detached but usable, since
    ``expire_on_commit=False``) ``CurrentAuth``. Requires an ``active`` session.

    Raises:
        AuthRequiredError: Missing/unknown/revoked/expired cookie.
        SessionStateError: Session exists but is not ``active``.
    """
    raw_token = request.cookies.get(COOKIE_NAME)
    if raw_token is None:
        raise AuthRequiredError()
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    settings: Settings = request.app.state.settings
    async with sessionmaker() as db:
        found = await AuthService(db, settings).get_valid_session(raw_token)
    if found is None:
        raise AuthRequiredError()
    session, user = found
    if session.state != SessionState.ACTIVE:
        raise SessionStateError("This endpoint requires a fully authenticated session.")
    return CurrentAuth(session=session, user=user, raw_token=raw_token)
