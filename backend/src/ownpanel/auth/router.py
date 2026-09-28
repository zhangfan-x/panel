"""/bootstrap/* and /auth/* endpoints (mounted under /api/v1 by the app factory).

Response field names are part of the frontend contract; do not rename them.
Failure-path audit rows are committed independently (``commit=True``) because
the surrounding request transaction is rolled back when the error propagates;
success paths also commit because the request-scoped session is closed without
an implicit commit.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ownpanel.audit import record_audit
from ownpanel.auth.deps import (
    COOKIE_NAME,
    CurrentAuth,
    get_auth_service,
    get_db,
    get_limiters,
    get_settings_from_app,
    require_active_session,
    require_csrf,
    require_recent_reauth,
    require_session,
)
from ownpanel.auth.service import AuthService
from ownpanel.config import Settings
from ownpanel.db.models import SessionState
from ownpanel.errors import AppError
from ownpanel.security import SlidingWindowLimiter

bootstrap_router = APIRouter(prefix="/bootstrap", tags=["bootstrap"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings_from_app)]
LimitersDep = Annotated[dict[str, SlidingWindowLimiter], Depends(get_limiters)]
ServiceDep = Annotated[AuthService, Depends(get_auth_service)]
SessionDep = Annotated[CurrentAuth, Depends(require_session)]
ActiveSessionDep = Annotated[CurrentAuth, Depends(require_active_session)]

# Length caps are defensive only (Argon2 cost, absurd payloads); real validation
# lives in the service layer. Passwords can only ever be set through these
# endpoints, so the 128-char cap is globally consistent.
_PASSWORD_MAX = 128


class BootstrapOwnerRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX)


class ReauthRequest(BaseModel):
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=_PASSWORD_MAX)
    new_password: str = Field(min_length=1, max_length=_PASSWORD_MAX)


def _client_meta(request: Request) -> tuple[str, str]:
    ip = request.client.host if request.client is not None else ""
    return ip, request.headers.get("user-agent", "")


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.limits.session_max_hours * 3600,
        path="/",
        httponly=True,
        samesite="strict",
        secure=not settings.server.dev_http,
    )


@bootstrap_router.get("/state")
async def bootstrap_state(service: ServiceDep) -> dict[str, Any]:
    """Public setup probe: true until the owner account exists."""
    return {"needs_setup": not await service.owner_exists()}


@bootstrap_router.post("/owner")
async def bootstrap_owner(
    body: BootstrapOwnerRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    service: ServiceDep,
) -> dict[str, Any]:
    """Create the owner account with a bootstrap token; sets the session cookie."""
    ip, user_agent = _client_meta(request)
    try:
        user, token = await service.create_owner(
            body.token, body.username, body.password, ip=ip, user_agent=user_agent
        )
    except AppError as exc:
        await record_audit(
            db,
            resource="bootstrap",
            action="bootstrap.create_owner",
            result="denied",
            target=body.username,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    _set_session_cookie(response, token, settings)
    await record_audit(
        db,
        resource="bootstrap",
        action="bootstrap.create_owner",
        actor_user_id=user.id,
        target=user.username,
        request=request,
        commit=True,
    )
    return {"session_state": SessionState.ACTIVE.value}


@auth_router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    service: ServiceDep,
    limiters: LimitersDep,
) -> dict[str, Any]:
    """Password login; sets the session cookie on success."""
    ip, user_agent = _client_meta(request)
    try:
        limiters["login_ip"].acquire(("login_ip", ip))
        limiters["login_user"].acquire(("login_user", body.username))
        session, token = await service.login(
            body.username, body.password, ip=ip, user_agent=user_agent
        )
    except AppError as exc:
        await record_audit(
            db,
            resource="auth",
            action="auth.login",
            result="denied",
            target=body.username,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    limiters["login_ip"].reset(("login_ip", ip))
    limiters["login_user"].reset(("login_user", body.username))
    _set_session_cookie(response, token, settings)
    await record_audit(
        db,
        resource="auth",
        action="auth.login",
        actor_user_id=session.user_id,
        target=body.username,
        request=request,
        commit=True,
    )
    return {"session_state": session.state}


@auth_router.get("/session")
async def session_info(auth: SessionDep, service: ServiceDep) -> dict[str, Any]:
    """Current user, session state, CSRF token and re-auth freshness."""
    return {
        "user": {"id": auth.user.id, "username": auth.user.username},
        "session_state": auth.session.state,
        "csrf_token": service.csrf_token_for(auth.session),
        "reauth_recent": service.has_recent_reauth(auth.session),
    }


@auth_router.post("/logout", status_code=204, dependencies=[Depends(require_csrf)])
async def logout(
    request: Request,
    response: Response,
    auth: SessionDep,
    db: DbDep,
    settings: SettingsDep,
    service: ServiceDep,
) -> None:
    """Revoke the current session and clear the cookie."""
    await service.logout(auth.session)
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
        secure=not settings.server.dev_http,
    )
    await record_audit(
        db,
        resource="auth",
        action="auth.logout",
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )


@auth_router.post("/reauth", dependencies=[Depends(require_csrf)])
async def reauth(
    body: ReauthRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    service: ServiceDep,
    limiters: LimitersDep,
) -> dict[str, Any]:
    """Re-verify the password to open the re-authentication window."""
    key = ("reauth", str(auth.user.id))
    try:
        limiters["reauth"].acquire(key)
        await service.reauth(auth.session, auth.user, body.password)
    except AppError as exc:
        await record_audit(
            db,
            resource="auth",
            action="auth.reauth",
            result="denied",
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    limiters["reauth"].reset(key)
    await record_audit(
        db,
        resource="auth",
        action="auth.reauth",
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"reauth_recent": True}


@auth_router.get("/sessions")
async def list_sessions(auth: ActiveSessionDep, service: ServiceDep) -> dict[str, Any]:
    """List the user's live sessions; the current one is flagged."""
    sessions = await service.list_sessions(auth.user, auth.session.token_hash)
    return {"sessions": sessions}


@auth_router.delete("/sessions/{session_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def revoke_session(
    session_id: int,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    service: ServiceDep,
) -> None:
    """Revoke one of the user's sessions by id."""
    await service.revoke_session(auth.user, session_id)
    await record_audit(
        db,
        resource="auth",
        action="auth.session_revoke",
        actor_user_id=auth.user.id,
        target=str(session_id),
        request=request,
        commit=True,
    )


@auth_router.post(
    "/password",
    status_code=204,
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    service: ServiceDep,
) -> None:
    """Change the password; all other sessions of the user are revoked."""
    await service.change_password(auth.user, auth.session, body.current_password, body.new_password)
    await record_audit(
        db,
        resource="auth",
        action="auth.password_change",
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
