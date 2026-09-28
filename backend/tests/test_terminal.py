"""Tests for the root terminal domain (docs/code-plan-m2-m5.md §4.7/§5).

Covers the in-memory ticket manager, the ``POST /terminal/session`` guards
(CSRF / recent re-auth / service reachability), and the manual WebSocket
Origin/session/ticket validation. The real root PTY service is never started:
the terminal socket simply does not exist, so ``probe`` fails with 503, and the
WebSocket rejection paths all trigger before any PTY is opened.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    API,
    OwnerContext,
    SettingsFactory,
    csrf_headers,
    db_execute,
    naive_utcnow,
)
from fastapi import FastAPI
from sqlalchemy import update
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ownpanel.app import create_app
from ownpanel.auth.deps import COOKIE_NAME
from ownpanel.db import run_migrations
from ownpanel.db.models import Session
from ownpanel.security import generate_token, hash_token
from ownpanel.terminal.manager import TerminalManager

SESSION_URL = f"{API}/terminal/session"
WS_PATH = f"{API}/terminal/ws"
GOOD_ORIGIN = "http://testserver"


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


# --- ticket manager unit tests ------------------------------------------------


def test_manager_not_active_initially(settings: Any) -> None:
    manager = TerminalManager(settings)
    assert manager.has_active() is False
    manager.issue_ticket()
    # Issuing a ticket does not open a PTY, so nothing is active yet.
    assert manager.has_active() is False


def test_ticket_single_use(settings: Any) -> None:
    manager = TerminalManager(settings)
    ticket, session_id = manager.issue_ticket()
    assert manager.consume_ticket(ticket) == session_id
    assert manager.consume_ticket(ticket) is None


def test_ticket_wrong_value_does_not_consume(settings: Any) -> None:
    manager = TerminalManager(settings)
    ticket, session_id = manager.issue_ticket()
    assert manager.consume_ticket("not-the-ticket") is None
    # A wrong guess must not burn the still-valid ticket.
    assert manager.consume_ticket(ticket) == session_id


def test_ticket_expired(settings: Any) -> None:
    manager = TerminalManager(settings)
    ticket, _session_id = manager.issue_ticket()
    assert manager._ticket is not None
    manager._ticket.expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
    assert manager.consume_ticket(ticket) is None


def test_ticket_none_when_never_issued(settings: Any) -> None:
    manager = TerminalManager(settings)
    assert manager.consume_ticket("anything") is None


# --- POST /terminal/session ---------------------------------------------------


async def test_session_requires_csrf(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.post(SESSION_URL)
    assert_problem(resp, 403, "csrf.invalid")


async def test_session_requires_recent_reauth(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    # A freshly bootstrapped session has reauth_at unset, so the reauth gate trips.
    resp = await client.post(SESSION_URL, headers=csrf_headers(owner))
    assert_problem(resp, 403, "auth.reauth_required")


async def test_session_503_when_service_unreachable(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    # Open the reauth window so the request reaches the terminal-service probe.
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow()))
    resp = await client.post(SESSION_URL, headers=csrf_headers(owner))
    # The terminal socket does not exist (no root service running) -> 503.
    assert_problem(resp, 503, "terminal.unavailable")


# --- WebSocket manual validation ---------------------------------------------


def _sqlite_ts(dt: datetime) -> str:
    return dt.isoformat(sep=" ", timespec="microseconds")


def _seed_active_session(db_path: Path, token_hash: str) -> None:
    """Insert an owner and one active, unexpired session with the given token hash."""
    now = naive_utcnow()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "INSERT INTO users (id, username, password_hash, created_at, password_changed_at) "
            "VALUES (1, 'owner', 'x', ?, ?)",
            (_sqlite_ts(now), _sqlite_ts(now)),
        )
        con.execute(
            "INSERT INTO sessions (token_hash, user_id, state, created_at, last_seen_at, "
            "absolute_expires_at, reauth_at, revoked_at, ip, user_agent) "
            "VALUES (?, 1, 'active', ?, ?, ?, NULL, NULL, '', '')",
            (
                token_hash,
                _sqlite_ts(now),
                _sqlite_ts(now),
                _sqlite_ts(now + timedelta(hours=12)),
            ),
        )
        con.commit()
    finally:
        con.close()


def _build_ws_app(make_settings: SettingsFactory) -> tuple[FastAPI, str]:
    """Build an app whose Host allowlist includes 'testserver' and seed a session."""
    settings = make_settings(server={"external_hosts": ["testserver"]})
    run_migrations(settings)
    raw_token = generate_token()
    _seed_active_session(settings.database_path, hash_token(raw_token))
    return create_app(settings), raw_token


def test_ws_rejects_bad_origin(make_settings: SettingsFactory) -> None:
    app, raw_token = _build_ws_app(make_settings)
    headers = {"Origin": "http://evil.example", "Cookie": f"{COOKIE_NAME}={raw_token}"}
    with TestClient(app) as tc:
        connect = tc.websocket_connect(WS_PATH, headers=headers)
        with pytest.raises(WebSocketDisconnect) as exc, connect:
            pass
        assert exc.value.code == 1008


def test_ws_rejects_missing_cookie(make_settings: SettingsFactory) -> None:
    app, _raw_token = _build_ws_app(make_settings)
    headers = {"Origin": GOOD_ORIGIN}
    with TestClient(app) as tc:
        connect = tc.websocket_connect(WS_PATH, headers=headers)
        with pytest.raises(WebSocketDisconnect) as exc, connect:
            pass
        assert exc.value.code == 1008


def test_ws_rejects_bad_ticket(make_settings: SettingsFactory) -> None:
    app, raw_token = _build_ws_app(make_settings)
    headers = {"Origin": GOOD_ORIGIN, "Cookie": f"{COOKIE_NAME}={raw_token}"}
    with TestClient(app) as tc, tc.websocket_connect(WS_PATH, headers=headers) as ws:
        ws.send_json({"type": "auth", "ticket": "bogus-ticket"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008


def test_ws_rejects_non_auth_first_frame(make_settings: SettingsFactory) -> None:
    app, raw_token = _build_ws_app(make_settings)
    headers = {"Origin": GOOD_ORIGIN, "Cookie": f"{COOKIE_NAME}={raw_token}"}
    with TestClient(app) as tc, tc.websocket_connect(WS_PATH, headers=headers) as ws:
        ws.send_json({"type": "input", "data": "whoami"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008
