"""root terminal API (code-plan-m2-m5 §4.7/§5.2, prefix ``/terminal``).

``POST /terminal/session`` issues a one-time WebSocket ticket (CSRF + recent
re-auth required); ``WS /terminal/ws`` bridges a browser terminal to a root PTY.
The WebSocket handler validates Origin/Host/session **manually** because the
pure-ASGI Host/Origin middleware only processes ``http`` scopes (spec §0.6).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy import update
from starlette.websockets import WebSocket, WebSocketDisconnect

from ownpanel.audit import record_audit
from ownpanel.auth.deps import (
    COOKIE_NAME,
    ActiveSessionDep,
    CurrentAuth,
    DbDep,
    require_csrf,
    require_recent_reauth,
)
from ownpanel.auth.service import AuthService
from ownpanel.config import Settings
from ownpanel.db.models import SessionState, TerminalSession
from ownpanel.errors import AppError, ConflictError, TerminalUnavailableError
from ownpanel.logging import get_logger
from ownpanel.terminal.client import TerminalServiceError
from ownpanel.terminal.manager import TerminalManager

terminal_router = APIRouter(prefix="/terminal", tags=["terminal"])

log = get_logger(__name__)

_WS_REJECT_CODE = 1008  # policy violation
_WS_INTERNAL_CODE = 1011  # internal error
_AUTH_FRAME_TIMEOUT_S = 10.0
_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24


def _manager(app_state_holder: Request | WebSocket) -> TerminalManager:
    manager: TerminalManager = app_state_holder.app.state.terminal
    return manager


@terminal_router.post(
    "/session", dependencies=[Depends(require_csrf), Depends(require_recent_reauth)]
)
async def create_session(request: Request, auth: ActiveSessionDep, db: DbDep) -> dict[str, str]:
    """Issue a one-time ticket for a terminal WebSocket and record the session row."""
    manager = _manager(request)
    ip = request.client.host if request.client is not None else ""
    try:
        if manager.has_active():
            raise ConflictError("A terminal session is already active.")
        # Fail fast (503) if the root terminal service is unreachable, so the SPA
        # can render a disabled state instead of a fake session.
        await manager.probe()
        ticket, session_id = manager.issue_ticket()
        db.add(
            TerminalSession(
                id=session_id,
                user_id=auth.user.id,
                created_at=datetime.now(tz=UTC),
                ip=ip,
            )
        )
        await record_audit(
            db,
            resource="terminal",
            action="terminal.session_create",
            target=session_id,
            actor_user_id=auth.user.id,
            request=request,
            commit=True,
        )
    except AppError as exc:
        await record_audit(
            db,
            resource="terminal",
            action="terminal.session_create",
            result="denied",
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    return {"ticket": ticket, "session_id": session_id}


def _origin_allowed(websocket: WebSocket, settings: Settings) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        return False
    return origin.lower() in {o.lower() for o in settings.allowed_origins()}


def _host_allowed(websocket: WebSocket, settings: Settings) -> bool:
    host = websocket.headers.get("host")
    if host is None:
        return False
    return host.lower() in {h.lower() for h in settings.allowed_hosts()}


async def _resolve_ws_auth(websocket: WebSocket) -> CurrentAuth | None:
    """Validate the session cookie for the WebSocket; None means reject.

    Mirrors :func:`ownpanel.auth.deps.resolve_stream_auth` but reads the cookie
    off the WebSocket and requires an ``active`` session.
    """
    raw_token = websocket.cookies.get(COOKIE_NAME)
    if raw_token is None:
        return None
    sessionmaker = websocket.app.state.sessionmaker
    settings: Settings = websocket.app.state.settings
    async with sessionmaker() as db:
        found = await AuthService(db, settings).get_valid_session(raw_token)
    if found is None:
        return None
    session, user = found
    if session.state != SessionState.ACTIVE:
        return None
    return CurrentAuth(session=session, user=user, raw_token=raw_token)


async def _consume_first_ticket(websocket: WebSocket, manager: TerminalManager) -> str | None:
    """First frame must be ``{"type":"auth","ticket":...}``; returns session_id or None."""
    try:
        text = await asyncio.wait_for(websocket.receive_text(), timeout=_AUTH_FRAME_TIMEOUT_S)
    except (TimeoutError, WebSocketDisconnect, KeyError, RuntimeError):
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("type") != "auth":
        return None
    ticket = message.get("ticket")
    if not isinstance(ticket, str):
        return None
    return manager.consume_ticket(ticket)


@terminal_router.websocket("/ws")
async def terminal_ws(websocket: WebSocket) -> None:
    """Bridge a browser terminal to a root PTY after manual Origin/session checks."""
    settings: Settings = websocket.app.state.settings
    manager = _manager(websocket)

    if not _host_allowed(websocket, settings) or not _origin_allowed(websocket, settings):
        await websocket.close(code=_WS_REJECT_CODE)
        return
    auth = await _resolve_ws_auth(websocket)
    if auth is None:
        await websocket.close(code=_WS_REJECT_CODE)
        return

    await websocket.accept()

    session_id = await _consume_first_ticket(websocket, manager)
    if session_id is None:
        await websocket.close(code=_WS_REJECT_CODE)
        return

    limits = settings.limits
    idle_s = limits.terminal_idle_minutes * 60.0
    max_s = limits.terminal_max_minutes * 60.0
    started = datetime.now(tz=UTC)
    master_fd: int | None = None
    opened = False
    exit_status: int | None = None
    try:
        _pid, master_fd = await manager.open_pty(cols=_DEFAULT_COLS, rows=_DEFAULT_ROWS)
        opened = True

        async def _still_valid() -> bool:
            """True while the authorizing session is still valid and active."""
            sessionmaker = websocket.app.state.sessionmaker
            async with sessionmaker() as db:
                found = await AuthService(db, settings).get_valid_session(auth.raw_token)
            return found is not None and found[0].state == SessionState.ACTIVE

        await manager.bridge(
            websocket,
            master_fd,
            idle_timeout_s=idle_s,
            max_duration_s=max_s,
            revalidate=_still_valid,
        )
    except (TerminalUnavailableError, ConflictError, TerminalServiceError) as exc:
        log.warning("terminal.ws_open_failed", error=str(exc))
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "message": "terminal unavailable"})
    except WebSocketDisconnect:
        pass
    finally:
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        if opened:
            exit_status = await manager.close_pty()
        with contextlib.suppress(Exception):
            await websocket.close(code=1000)
        await _record_close(
            websocket, auth=auth, session_id=session_id, started=started, exit_status=exit_status
        )


async def _record_close(
    websocket: WebSocket,
    *,
    auth: CurrentAuth,
    session_id: str,
    started: datetime,
    exit_status: int | None,
) -> None:
    now = datetime.now(tz=UTC)
    duration_ms = int((now - started).total_seconds() * 1000)
    sessionmaker = websocket.app.state.sessionmaker
    async with sessionmaker() as db:
        await db.execute(
            update(TerminalSession)
            .where(TerminalSession.id == session_id)
            .values(closed_at=now, exit_status=exit_status, duration_ms=duration_ms)
        )
        # record_audit only reads HTTPConnection attributes (state/client/headers),
        # all present on a WebSocket; the cast keeps mypy happy.
        await record_audit(
            db,
            resource="terminal",
            action="terminal.session_close",
            target=session_id,
            actor_user_id=auth.user.id,
            request=cast("Request", websocket),
            duration_ms=duration_ms,
            commit=True,
        )
