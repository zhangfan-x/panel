"""Login, session lifecycle, re-auth and password change (code-plan §5).

Second sessions are created through extra ``httpx.AsyncClient`` instances that
share the same ASGI app but keep their own cookie jars ("second browser").
Session/re-auth expiry is exercised by backdating rows in the database (naive
UTC, the SQLite storage form) instead of sleeping.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from conftest import (
    API,
    BASE_URL,
    ORIGIN,
    OwnerContext,
    csrf_headers,
    db_execute,
    db_scalar,
    naive_utcnow,
)
from fastapi import FastAPI
from sqlalchemy import select, update

from ownpanel.auth.deps import COOKIE_NAME
from ownpanel.db.models import Session
from ownpanel.security import generate_token

LOGIN = f"{API}/auth/login"
SESSION_INFO = f"{API}/auth/session"
SESSIONS = f"{API}/auth/sessions"
LOGOUT = f"{API}/auth/logout"
REAUTH = f"{API}/auth/reauth"
PASSWORD = f"{API}/auth/password"

NEW_PASSWORD = "rotated-Passw0rd-42!"


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


def extra_client(app: FastAPI) -> httpx.AsyncClient:
    """A second browser: separate cookie jar over the same (already live) app."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Origin": ORIGIN},
    )


async def login_to_active(http: httpx.AsyncClient, owner: OwnerContext) -> str:
    """Password login on ``http``; returns the new session's CSRF token."""
    resp = await http.post(LOGIN, json={"username": owner.username, "password": owner.password})
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_state"] == "active"
    resp = await http.get(SESSION_INFO)
    assert resp.status_code == 200
    token: str = resp.json()["csrf_token"]
    return token


# --- login ---------------------------------------------------------------------------


async def test_login_creates_active_session(app, client, owner):
    async with extra_client(app) as http:
        resp = await http.post(LOGIN, json={"username": owner.username, "password": owner.password})
        assert resp.status_code == 200
        assert resp.json() == {"session_state": "active"}
        assert http.cookies.get(COOKIE_NAME) is not None

        body = (await http.get(SESSION_INFO)).json()
        assert body["session_state"] == "active"
        assert isinstance(body["csrf_token"], str) and body["csrf_token"]
        assert body["user"]["username"] == owner.username
        assert body["reauth_recent"] is False
        assert (await http.get(SESSIONS)).status_code == 200


async def test_unknown_user_and_wrong_password_same_error(client, owner):
    unknown = await client.post(
        LOGIN, json={"username": "no-such-user", "password": "wrong-pass-123456"}
    )
    wrong = await client.post(
        LOGIN, json={"username": owner.username, "password": "wrong-pass-123456"}
    )
    unknown_body = assert_problem(unknown, 401, "auth.invalid_credentials")
    wrong_body = assert_problem(wrong, 401, "auth.invalid_credentials")
    # Identical bodies apart from the per-request id: no username oracle.
    unknown_body.pop("request_id", None)
    wrong_body.pop("request_id", None)
    assert unknown_body == wrong_body


# --- session guards and expiry ------------------------------------------------------


async def test_missing_and_forged_cookie_are_401(client):
    assert_problem(await client.get(SESSION_INFO), 401, "auth.required")
    assert_problem(await client.get(SESSIONS), 401, "auth.required")

    forged = {"Cookie": f"{COOKIE_NAME}={generate_token()}"}
    assert_problem(await client.get(SESSION_INFO, headers=forged), 401, "auth.required")
    assert_problem(await client.get(SESSIONS, headers=forged), 401, "auth.required")


async def test_tampered_cookie_is_401(client, owner):
    real = client.cookies.get(COOKIE_NAME)
    assert real is not None
    tampered = real[:-1] + ("A" if real[-1] != "A" else "B")
    resp = await client.get(SESSION_INFO, headers={"Cookie": f"{COOKIE_NAME}={tampered}"})
    assert_problem(resp, 401, "auth.required")
    # The untouched genuine cookie still works.
    assert (await client.get(SESSION_INFO)).status_code == 200


async def test_idle_expiry_after_30_minutes(app, client, owner):
    assert (await client.get(SESSION_INFO)).status_code == 200
    await db_execute(
        app, update(Session).values(last_seen_at=naive_utcnow() - timedelta(minutes=31))
    )
    assert_problem(await client.get(SESSION_INFO), 401, "auth.required")


async def test_absolute_expiry(app, client, owner):
    # last_seen_at is fresh, only the absolute deadline is in the past.
    await db_execute(
        app, update(Session).values(absolute_expires_at=naive_utcnow() - timedelta(minutes=1))
    )
    assert_problem(await client.get(SESSION_INFO), 401, "auth.required")


async def test_logout_revokes_session_and_clears_cookie(app, client, owner):
    resp = await client.post(LOGOUT, headers=csrf_headers(owner))
    assert resp.status_code == 204
    set_cookie = resp.headers["set-cookie"].lower()
    assert "ownpanel_session=" in set_cookie
    assert "max-age=0" in set_cookie

    assert await db_scalar(app, select(Session.revoked_at)) is not None
    assert client.cookies.get(COOKIE_NAME) is None
    assert_problem(await client.get(SESSION_INFO), 401, "auth.required")


# --- session listing and revocation -------------------------------------------------


async def test_sessions_list_marks_current(app, client, owner):
    async with extra_client(app) as http:
        await login_to_active(http, owner)

        resp = await client.get(SESSIONS)
        assert resp.status_code == 200
        here = resp.json()["sessions"]
        assert len(here) == 2
        assert {"id", "created_at", "last_seen_at", "ip", "user_agent", "current"} <= set(here[0])
        assert [s["current"] for s in here].count(True) == 1

        # The other client sees the same two sessions with the flag on the other one.
        there = (await http.get(SESSIONS)).json()["sessions"]
        assert {s["id"] for s in there} == {s["id"] for s in here}
        assert {s["id"] for s in there if s["current"]} != {s["id"] for s in here if s["current"]}


async def test_revoke_other_session(app, client, owner):
    async with extra_client(app) as http:
        await login_to_active(http, owner)
        assert (await http.get(SESSION_INFO)).status_code == 200

        sessions = (await client.get(SESSIONS)).json()["sessions"]
        other_id = next(s["id"] for s in sessions if not s["current"])

        resp = await client.delete(f"{SESSIONS}/{other_id}", headers=csrf_headers(owner))
        assert resp.status_code == 204

        # The revoked session is dead, the revoking one still lives.
        assert_problem(await http.get(SESSION_INFO), 401, "auth.required")
        assert (await client.get(SESSION_INFO)).status_code == 200

        resp = await client.delete(f"{SESSIONS}/999999", headers=csrf_headers(owner))
        assert_problem(resp, 404, "not_found")


# --- re-authentication and password change ------------------------------------------


async def test_change_password_requires_recent_reauth(client, owner):
    resp = await client.post(
        PASSWORD,
        json={"current_password": owner.password, "new_password": NEW_PASSWORD},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 403, "auth.reauth_required")


async def test_change_password_full_flow(app, client, owner):
    async with extra_client(app) as http:
        await login_to_active(http, owner)

        resp = await client.post(
            REAUTH,
            json={"password": owner.password},
            headers=csrf_headers(owner),
        )
        assert resp.status_code == 200
        assert resp.json() == {"reauth_recent": True}
        assert (await client.get(SESSION_INFO)).json()["reauth_recent"] is True

        # Wrong current password / weak new password fail inside the window.
        resp = await client.post(
            PASSWORD,
            json={"current_password": "definitely-wrong-1", "new_password": NEW_PASSWORD},
            headers=csrf_headers(owner),
        )
        assert_problem(resp, 401, "auth.invalid_credentials")
        resp = await client.post(
            PASSWORD,
            json={"current_password": owner.password, "new_password": "alllowercaseonly"},
            headers=csrf_headers(owner),
        )
        assert_problem(resp, 422, "auth.weak_password")

        resp = await client.post(
            PASSWORD,
            json={"current_password": owner.password, "new_password": NEW_PASSWORD},
            headers=csrf_headers(owner),
        )
        assert resp.status_code == 204

        # The other session was revoked; the changing session survives.
        assert_problem(await http.get(SESSION_INFO), 401, "auth.required")
        resp = await client.get(SESSION_INFO)
        assert resp.status_code == 200
        assert resp.json()["session_state"] == "active"

    async with extra_client(app) as fresh:
        resp = await fresh.post(
            LOGIN, json={"username": owner.username, "password": owner.password}
        )
        assert_problem(resp, 401, "auth.invalid_credentials")
        resp = await fresh.post(LOGIN, json={"username": owner.username, "password": NEW_PASSWORD})
        assert resp.status_code == 200
        assert resp.json() == {"session_state": "active"}


async def test_reauth_negative_paths_and_window_expiry(app, client, owner):
    resp = await client.post(
        REAUTH,
        json={"password": "wrong-password-99"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 401, "auth.invalid_credentials")
    assert (await client.get(SESSION_INFO)).json()["reauth_recent"] is False

    resp = await client.post(
        REAUTH,
        json={"password": owner.password},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200

    # 61 seconds later (backdated, no sleeping) the window is shut again.
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow() - timedelta(seconds=61)))
    resp = await client.post(
        PASSWORD,
        json={"current_password": owner.password, "new_password": NEW_PASSWORD},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 403, "auth.reauth_required")
    assert (await client.get(SESSION_INFO)).json()["reauth_recent"] is False
