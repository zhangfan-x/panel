"""Login state machine, session lifecycle, re-auth and TOTP rebind (code-plan §5).

Second sessions are created through extra ``httpx.AsyncClient`` instances that
share the same ASGI app but keep their own cookie jars ("second browser").
Session/re-auth expiry is exercised by backdating rows in the database (naive
UTC, the SQLite storage form) instead of sleeping.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import httpx
import pyotp
from conftest import (
    API,
    BASE_URL,
    ORIGIN,
    OWNER_PASSWORD,
    OWNER_USERNAME,
    OwnerContext,
    csrf_headers,
    db_execute,
    db_scalar,
    naive_utcnow,
)
from fastapi import FastAPI
from sqlalchemy import func, select, update

from mypanel.auth.deps import COOKIE_NAME
from mypanel.auth.service import AuthService
from mypanel.db.models import MfaDevice, Session
from mypanel.security import generate_token

BOOTSTRAP_OWNER = f"{API}/bootstrap/owner"
LOGIN = f"{API}/auth/login"
VERIFY = f"{API}/auth/totp/verify"
SETUP = f"{API}/auth/totp/setup"
CONFIRM = f"{API}/auth/totp/confirm"
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


def totp_now(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def wrong_totp_code(secret: str) -> str:
    """A six-digit code guaranteed invalid for ``secret`` around the current time.

    The server accepts one 30-second step of drift, so exclude the codes of the
    surrounding five steps and return the smallest six-digit string left over.
    """
    totp = pyotp.TOTP(secret)
    now = time.time()
    valid = {totp.at(int(now) + offset) for offset in range(-60, 61, 30)}
    candidate = 0
    while f"{candidate:06d}" in valid:
        candidate += 1
    return f"{candidate:06d}"


def extra_client(app: FastAPI) -> httpx.AsyncClient:
    """A second browser: separate cookie jar over the same (already live) app."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Origin": ORIGIN},
    )


async def login_to_active(http: httpx.AsyncClient, owner: OwnerContext) -> str:
    """Full password+TOTP login on ``http``; returns the new session's CSRF token."""
    resp = await http.post(LOGIN, json={"username": owner.username, "password": owner.password})
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_state"] == "mfa_pending"
    resp = await http.post(VERIFY, json={"code": totp_now(owner.totp_secret)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_state"] == "active"
    resp = await http.get(SESSION_INFO)
    assert resp.status_code == 200
    token: str = resp.json()["csrf_token"]
    return token


async def bootstrap_owner_without_totp(app: FastAPI, http: httpx.AsyncClient) -> None:
    """Create the owner via /bootstrap/owner only (session stays totp_enroll)."""
    async with app.state.sessionmaker() as db:
        token = await AuthService(db, app.state.settings).issue_bootstrap_token()
    resp = await http.post(
        BOOTSTRAP_OWNER,
        json={"token": token, "username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )
    assert resp.status_code == 200, resp.text


# --- login state machine ------------------------------------------------------------


async def test_login_state_machine_password_then_totp(app, client, owner):
    async with extra_client(app) as http:
        resp = await http.post(LOGIN, json={"username": owner.username, "password": owner.password})
        assert resp.status_code == 200
        assert resp.json() == {"session_state": "mfa_pending"}
        assert http.cookies.get(COOKIE_NAME) is not None

        # mfa_pending is fenced off active-only endpoints and TOTP enrolment.
        assert_problem(await http.get(SESSIONS), 403, "auth.session_state")
        assert_problem(await http.post(SETUP), 403, "auth.session_state")
        csrf = (await http.get(SESSION_INFO)).json()["csrf_token"]
        resp = await http.post(
            REAUTH,
            json={"password": owner.password, "code": totp_now(owner.totp_secret)},
            headers={"X-CSRF-Token": csrf},
        )
        assert_problem(resp, 403, "auth.session_state")

        # Wrong code does not upgrade the session.
        resp = await http.post(VERIFY, json={"code": wrong_totp_code(owner.totp_secret)})
        assert_problem(resp, 401, "auth.invalid_totp")
        assert (await http.get(SESSION_INFO)).json()["session_state"] == "mfa_pending"

        resp = await http.post(VERIFY, json={"code": totp_now(owner.totp_secret)})
        assert resp.status_code == 200
        assert resp.json() == {"session_state": "active"}
        assert (await http.get(SESSIONS)).status_code == 200


async def test_login_without_confirmed_device_enters_totp_enroll(app, client):
    await bootstrap_owner_without_totp(app, client)

    async with extra_client(app) as http:
        resp = await http.post(LOGIN, json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD})
        assert resp.status_code == 200
        assert resp.json() == {"session_state": "totp_enroll"}

        # No mfa_pending session: the TOTP login step is not available here...
        assert_problem(await http.post(VERIFY, json={"code": "123456"}), 403, "auth.session_state")
        # ...and confirming before setup has nothing to confirm.
        assert_problem(await http.post(CONFIRM, json={"code": "123456"}), 404, "not_found")

        resp = await http.post(SETUP)
        assert resp.status_code == 200
        secret = resp.json()["secret"]
        assert resp.json()["otpauth_uri"].startswith("otpauth://totp/")

        resp = await http.post(CONFIRM, json={"code": totp_now(secret)})
        assert resp.status_code == 200
        assert resp.json() == {"session_state": "active"}
        assert (await http.get(SESSION_INFO)).json()["session_state"] == "active"


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
    assert "mypanel_session=" in set_cookie
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

        # login_to_active just spent the current TOTP code; a real user's next reauth
        # would use the next code. Clear the one-time-use counter to stand in for that.
        await db_execute(app, update(MfaDevice).values(last_timecode=None))
        resp = await client.post(
            REAUTH,
            json={"password": owner.password, "code": totp_now(owner.totp_secret)},
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
        assert resp.json() == {"session_state": "mfa_pending"}
        # Clear the one-time-use counter spent by the reauth above (a real authenticator
        # would supply the next code) so this fresh login can verify TOTP.
        await db_execute(app, update(MfaDevice).values(last_timecode=None))
        resp = await fresh.post(VERIFY, json={"code": totp_now(owner.totp_secret)})
        assert resp.status_code == 200


async def test_reauth_negative_paths_and_window_expiry(app, client, owner):
    resp = await client.post(
        REAUTH,
        json={"password": "wrong-password-99", "code": totp_now(owner.totp_secret)},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 401, "auth.invalid_credentials")
    resp = await client.post(
        REAUTH,
        json={"password": owner.password, "code": wrong_totp_code(owner.totp_secret)},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 401, "auth.invalid_totp")

    resp = await client.post(
        REAUTH,
        json={"password": owner.password, "code": totp_now(owner.totp_secret)},
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


async def test_totp_code_is_single_use_across_flows(app, client, owner):
    """A captured TOTP code cannot be replayed: once spent on the login step it is
    rejected at re-auth and at any further login within its window, breaking the
    single-captured-code takeover chain (RFC 6238 §5.2)."""
    code = totp_now(owner.totp_secret)

    async with extra_client(app) as attacker:
        resp = await attacker.post(
            LOGIN, json={"username": owner.username, "password": owner.password}
        )
        assert resp.json()["session_state"] == "mfa_pending"
        resp = await attacker.post(VERIFY, json={"code": code})
        assert resp.status_code == 200
        assert resp.json()["session_state"] == "active"

        # Replaying the same code to /auth/reauth (the gate before a password change)
        # is now rejected, so the code cannot be reused to escalate.
        csrf = (await attacker.get(SESSION_INFO)).json()["csrf_token"]
        resp = await attacker.post(
            REAUTH,
            json={"password": owner.password, "code": code},
            headers={"X-CSRF-Token": csrf},
        )
        assert_problem(resp, 401, "auth.invalid_totp")

    # The same code also cannot drive a second fresh login within its window.
    async with extra_client(app) as second:
        resp = await second.post(
            LOGIN, json={"username": owner.username, "password": owner.password}
        )
        assert resp.json()["session_state"] == "mfa_pending"
        assert_problem(await second.post(VERIFY, json={"code": code}), 401, "auth.invalid_totp")


# --- GET /auth/session across states (frontend contract) ----------------------------


async def test_session_info_returns_csrf_in_every_state(app, client):
    await bootstrap_owner_without_totp(app, client)

    # totp_enroll
    resp = await client.get(SESSION_INFO)
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_state"] == "totp_enroll"
    assert isinstance(body["csrf_token"], str) and body["csrf_token"]
    assert body["user"]["username"] == OWNER_USERNAME
    assert body["reauth_recent"] is False

    secret = (await client.post(SETUP)).json()["secret"]
    assert (await client.post(CONFIRM, json={"code": totp_now(secret)})).status_code == 200

    # active
    body = (await client.get(SESSION_INFO)).json()
    assert body["session_state"] == "active"
    assert body["csrf_token"]

    # mfa_pending (fresh login in a second client)
    async with extra_client(app) as http:
        resp = await http.post(LOGIN, json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD})
        assert resp.json()["session_state"] == "mfa_pending"
        body = (await http.get(SESSION_INFO)).json()
        assert body["session_state"] == "mfa_pending"
        assert body["csrf_token"]


# --- TOTP rebind --------------------------------------------------------------------


async def test_totp_rebind_gate_and_secret_swap(app, client, owner):
    # Rebinding from an active session is gated on recent re-auth.
    assert_problem(await client.post(SETUP), 403, "auth.reauth_required")

    resp = await client.post(
        REAUTH,
        json={"password": owner.password, "code": totp_now(owner.totp_secret)},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200

    resp = await client.post(SETUP)
    assert resp.status_code == 200
    new_secret = resp.json()["secret"]
    assert new_secret != owner.totp_secret
    assert resp.json()["otpauth_uri"].startswith("otpauth://totp/")

    # Confirmation needs a code for the NEW secret; session stays active throughout.
    resp = await client.post(CONFIRM, json={"code": wrong_totp_code(new_secret)})
    assert_problem(resp, 401, "auth.invalid_totp")
    resp = await client.post(CONFIRM, json={"code": totp_now(new_secret)})
    assert resp.status_code == 200
    assert resp.json() == {"session_state": "active"}

    # Exactly one (confirmed) device remains and it carries the new secret...
    assert await db_scalar(app, select(func.count()).select_from(MfaDevice)) == 1
    assert await db_scalar(app, select(MfaDevice.secret)) == new_secret
    assert await db_scalar(app, select(MfaDevice.confirmed_at)) is not None

    # ...so old-secret codes no longer authenticate, new-secret codes do.
    resp = await client.post(
        REAUTH,
        json={"password": owner.password, "code": totp_now(owner.totp_secret)},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 401, "auth.invalid_totp")
    resp = await client.post(
        REAUTH,
        json={"password": owner.password, "code": totp_now(new_secret)},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
