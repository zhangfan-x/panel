"""Bootstrap flow (docs/code-plan.md section 5.1).

Covers GET /bootstrap/state, service-level ``issue_bootstrap_token`` (the CLI is a
thin wrapper around it) and every POST /bootstrap/owner outcome: wrong/expired/
replayed token, weak password, invalid username, success (cookie + totp_enroll
session, token destroyed) and the defensive 409 when an owner already exists.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import (
    API,
    BASE_URL,
    ORIGIN,
    OWNER_PASSWORD,
    OWNER_USERNAME,
    db_execute,
    db_scalar,
    naive_utcnow,
)
from fastapi import FastAPI
from sqlalchemy import func, insert, select, update

from mypanel.auth.service import (
    BOOTSTRAP_TOKEN_EXPIRES_AT_KEY,
    BOOTSTRAP_TOKEN_HASH_KEY,
    AuthService,
)
from mypanel.db.models import Setting, User
from mypanel.errors import BootstrapDoneError
from mypanel.security import generate_token, hash_token

STATE = f"{API}/bootstrap/state"
OWNER = f"{API}/bootstrap/owner"
SESSION_INFO = f"{API}/auth/session"

GOOD_BODY = {"username": OWNER_USERNAME, "password": OWNER_PASSWORD}


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


async def issue_token(app: FastAPI) -> str:
    """Issue a bootstrap token at service level (exactly what the CLI does)."""
    async with app.state.sessionmaker() as db:
        token = await AuthService(db, app.state.settings).issue_bootstrap_token()
    return token


async def stored_setting(app: FastAPI, key: str) -> str | None:
    """JSON-decoded value of a settings row, or None when the row is gone."""
    raw = await db_scalar(app, select(Setting.value).where(Setting.key == key))
    return None if raw is None else json.loads(raw)


# --- /bootstrap/state ---------------------------------------------------------------


async def test_state_needs_setup_initially(client):
    resp = await client.get(STATE)
    assert resp.status_code == 200
    assert resp.json() == {"needs_setup": True}


async def test_state_false_after_owner_created(client, owner):
    resp = await client.get(STATE)
    assert resp.status_code == 200
    assert resp.json() == {"needs_setup": False}


# --- issue_bootstrap_token ----------------------------------------------------------


async def test_issue_token_stores_hash_and_15min_expiry(app, client):
    token = await issue_token(app)
    assert isinstance(token, str)
    assert len(token) >= 40  # 32 urlsafe bytes

    # Only the SHA-256 hash is persisted, alongside an ISO expiry ~15 minutes out.
    assert await stored_setting(app, BOOTSTRAP_TOKEN_HASH_KEY) == hash_token(token)
    expires_raw = await stored_setting(app, BOOTSTRAP_TOKEN_EXPIRES_AT_KEY)
    assert expires_raw is not None
    remaining = datetime.fromisoformat(expires_raw) - datetime.now(tz=UTC)
    assert timedelta(minutes=13) < remaining <= timedelta(minutes=15)


async def test_reissue_overwrites_previous_token(app, client):
    first = await issue_token(app)
    second = await issue_token(app)
    assert first != second

    resp = await client.post(OWNER, json={"token": first, **GOOD_BODY})
    assert_problem(resp, 403, "bootstrap.invalid_token")
    resp = await client.post(OWNER, json={"token": second, **GOOD_BODY})
    assert resp.status_code == 200


async def test_issue_token_refused_once_owner_exists(app, client, owner):
    with pytest.raises(BootstrapDoneError):
        await issue_token(app)


# --- POST /bootstrap/owner: rejection paths -----------------------------------------


async def test_owner_rejects_token_when_none_issued(client):
    resp = await client.post(OWNER, json={"token": generate_token(), **GOOD_BODY})
    assert_problem(resp, 403, "bootstrap.invalid_token")


async def test_owner_rejects_wrong_token(app, client):
    await issue_token(app)
    resp = await client.post(OWNER, json={"token": generate_token(), **GOOD_BODY})
    assert_problem(resp, 403, "bootstrap.invalid_token")
    # Failed attempt creates no user.
    resp = await client.get(STATE)
    assert resp.json() == {"needs_setup": True}


async def test_owner_rejects_expired_token(app, client):
    token = await issue_token(app)
    past = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
    await db_execute(
        app,
        update(Setting)
        .where(Setting.key == BOOTSTRAP_TOKEN_EXPIRES_AT_KEY)
        .values(value=json.dumps(past)),
    )
    resp = await client.post(OWNER, json={"token": token, **GOOD_BODY})
    assert_problem(resp, 403, "bootstrap.invalid_token")


@pytest.mark.parametrize("password", ["Sh0rt-pw!", "a" * 16, "1234567890123456"])
async def test_owner_rejects_weak_password_and_keeps_token(app, client, password):
    """Too short or single character class -> 422; the token survives the failure."""
    token = await issue_token(app)
    resp = await client.post(
        OWNER, json={"token": token, "username": OWNER_USERNAME, "password": password}
    )
    assert_problem(resp, 422, "auth.weak_password")
    assert await db_scalar(app, select(User.id)) is None

    # The same token still works with a strong password afterwards.
    resp = await client.post(OWNER, json={"token": token, **GOOD_BODY})
    assert resp.status_code == 200


@pytest.mark.parametrize("username", ["ab", "a" * 33, "bad name", "bäd"])
async def test_owner_rejects_invalid_username(app, client, username):
    token = await issue_token(app)
    resp = await client.post(
        OWNER, json={"token": token, "username": username, "password": OWNER_PASSWORD}
    )
    assert_problem(resp, 422, "request.validation")


# --- POST /bootstrap/owner: success and one-time semantics --------------------------


async def test_owner_success_sets_enroll_session_and_consumes_token(app, client, settings):
    token = await issue_token(app)
    resp = await client.post(OWNER, json={"token": token, **GOOD_BODY})
    assert resp.status_code == 200
    assert resp.json() == {"session_state": "totp_enroll"}

    set_cookie = resp.headers["set-cookie"].lower()
    assert "mypanel_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie
    assert f"max-age={settings.limits.session_max_hours * 3600}" in set_cookie
    assert "secure" not in set_cookie  # dev_http test settings

    # The cookie is a live totp_enroll session with a CSRF token.
    resp = await client.get(SESSION_INFO)
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_state"] == "totp_enroll"
    assert body["user"]["username"] == OWNER_USERNAME
    assert body["csrf_token"]

    # Token records destroyed, setup no longer needed.
    assert await stored_setting(app, BOOTSTRAP_TOKEN_HASH_KEY) is None
    assert await stored_setting(app, BOOTSTRAP_TOKEN_EXPIRES_AT_KEY) is None
    resp = await client.get(STATE)
    assert resp.json() == {"needs_setup": False}

    # One-time token: replaying it is an invalid-token 403.
    resp = await client.post(
        OWNER, json={"token": token, "username": "other", "password": OWNER_PASSWORD}
    )
    assert_problem(resp, 403, "bootstrap.invalid_token")


async def test_owner_conflict_when_owner_already_exists(app, client, owner):
    """Defence in depth: a planted valid token is still refused with 409 once an
    owner exists (the CLI itself refuses to issue one at that point)."""
    token = generate_token()
    now = naive_utcnow()
    future = (datetime.now(tz=UTC) + timedelta(minutes=15)).isoformat()
    await db_execute(
        app,
        insert(Setting).values(
            key=BOOTSTRAP_TOKEN_HASH_KEY, value=json.dumps(hash_token(token)), updated_at=now
        ),
    )
    await db_execute(
        app,
        insert(Setting).values(
            key=BOOTSTRAP_TOKEN_EXPIRES_AT_KEY, value=json.dumps(future), updated_at=now
        ),
    )
    resp = await client.post(
        OWNER, json={"token": token, "username": "second", "password": OWNER_PASSWORD}
    )
    assert_problem(resp, 409, "bootstrap.already_done")


async def test_concurrent_bootstrap_creates_single_owner(app, client):
    """Two bootstraps racing past the non-atomic owner_exists() pre-check with one
    valid token must still yield exactly one owner. The fixed owner primary key turns
    a lost race into a clean rejection (409/403), never a second owner or a 500."""
    token = await issue_token(app)

    async def attempt(username: str) -> int:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL, headers={"Origin": ORIGIN}
        ) as c:
            resp = await c.post(
                OWNER, json={"token": token, "username": username, "password": OWNER_PASSWORD}
            )
            return resp.status_code

    statuses = await asyncio.gather(attempt("alice"), attempt("bob"))

    assert statuses.count(200) == 1, f"expected exactly one success, got {statuses}"
    loser = next(s for s in statuses if s != 200)
    assert loser in (403, 409), f"loser should be a clean rejection, got {loser}"
    # Exactly one owner row exists — no second owner slipped through.
    assert await db_scalar(app, select(func.count()).select_from(User)) == 1
