"""CSRF enforcement on state-changing endpoints (docs/code-plan.md section 5.3).

All scenarios build on the ``owner`` fixture's ACTIVE session:

* missing / wrong / correct ``X-CSRF-Token`` on POST /auth/logout;
* every CSRF-guarded mutation (reauth, session revoke, password change) fails with
  ``csrf.invalid`` when the header is missing — for POST /auth/password the session
  also lacks a recent re-auth (``reauth_at`` is NULL on fresh sessions), so getting
  ``csrf.invalid`` rather than ``auth.reauth_required`` proves the CSRF dependency
  runs first;
* a valid CSRF token does not bypass the Host/Origin middleware: an evil or absent
  Origin is rejected with ``request.bad_origin`` before any handler runs;
* CSRF tokens are bound to their session — another session's token is rejected;
* an ``mfa_pending`` session can log itself out with its own token (logout is
  allowed in any session state);
* safe methods (GET) require no CSRF token at all.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pyotp
import pytest
from conftest import API, BASE_URL, ORIGIN, OWNER_PASSWORD, OwnerContext, csrf_headers
from fastapi import FastAPI

LOGOUT = f"{API}/auth/logout"
REAUTH = f"{API}/auth/reauth"
PASSWORD = f"{API}/auth/password"
SESSION = f"{API}/auth/session"
SESSIONS = f"{API}/auth/sessions"

CSRF_HEADER = "X-CSRF-Token"


def assert_problem(resp: httpx.Response, *, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status and code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code, f"expected code {code!r}, got {body['code']!r}"
    assert body["title"]
    return body


def make_extra_client(
    app: FastAPI,
    *,
    origin: str | None = ORIGIN,
    cookies: httpx.Cookies | None = None,
) -> httpx.AsyncClient:
    """Additional client against the same app (the shared ``client`` keeps the lifespan open).

    ``origin=None`` sends no Origin header at all; ``cookies`` seeds the jar, e.g. with the
    owner session cookie from the shared client.
    """
    headers = {} if origin is None else {"Origin": origin}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers=headers,
        cookies=cookies,
    )


async def login_mfa_pending(second: httpx.AsyncClient, owner: OwnerContext) -> str:
    """Password-only login on ``second``; returns the mfa_pending session's CSRF token."""
    resp = await second.post(
        f"{API}/auth/login", json={"username": owner.username, "password": owner.password}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_state"] == "mfa_pending"
    resp = await second.get(SESSION)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_state"] == "mfa_pending"
    token: str = body["csrf_token"]
    return token


# --- POST /auth/logout: missing / wrong / correct token -------------------------------


async def test_logout_without_csrf_token_rejected(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(LOGOUT)
    body = assert_problem(resp, status=403, code="csrf.invalid")
    assert body["title"] == "CSRF Validation Failed"
    # The rejected logout must not have revoked the session.
    resp = await client.get(SESSION)
    assert resp.status_code == 200
    assert resp.json()["session_state"] == "active"


async def test_logout_with_wrong_csrf_token_rejected(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    wrong = secrets.token_hex(32)  # well-formed 64-hex token, but not this session's
    assert wrong != owner.csrf_token
    resp = await client.post(LOGOUT, headers={CSRF_HEADER: wrong})
    assert_problem(resp, status=403, code="csrf.invalid")
    resp = await client.get(SESSION)
    assert resp.status_code == 200


async def test_logout_with_valid_csrf_token(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.post(LOGOUT, headers=csrf_headers(owner))
    assert resp.status_code == 204, resp.text
    # The session is revoked: the next authenticated call fails.
    resp = await client.get(SESSION)
    assert_problem(resp, status=401, code="auth.required")


# --- Missing token on the other CSRF-guarded mutations --------------------------------


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("POST", REAUTH, {"password": OWNER_PASSWORD, "code": "000000"}),
        ("DELETE", f"{API}/auth/sessions/999999", None),
        # No recent re-auth either — csrf.invalid (not auth.reauth_required) proves
        # the CSRF dependency is evaluated first.
        (
            "POST",
            PASSWORD,
            {"current_password": OWNER_PASSWORD, "new_password": "an0ther-long-secret!"},
        ),
    ],
    ids=["reauth", "revoke-session", "change-password"],
)
async def test_mutations_without_csrf_token_rejected(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    method: str,
    path: str,
    json_body: dict[str, str] | None,
) -> None:
    resp = await client.request(method, path, json=json_body)
    assert_problem(resp, status=403, code="csrf.invalid")


async def test_reauth_without_csrf_never_reaches_credentials(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    """Even fully correct credentials are ignored when the CSRF header is missing."""
    code = pyotp.TOTP(owner.totp_secret).now()
    resp = await client.post(REAUTH, json={"password": owner.password, "code": code})
    assert_problem(resp, status=403, code="csrf.invalid")
    # The re-auth window must not have opened.
    resp = await client.get(SESSION)
    assert resp.status_code == 200
    assert resp.json()["reauth_recent"] is False


# --- Valid CSRF cannot bypass the Host/Origin middleware ------------------------------


async def test_valid_csrf_with_evil_origin_rejected_by_middleware(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(LOGOUT, headers={**csrf_headers(owner), "Origin": "http://evil:5173"})
    body = assert_problem(resp, status=403, code="request.bad_origin")
    assert body["title"] == "Origin Not Allowed"
    # Rejected in middleware, before routing: the session survives.
    resp = await client.get(SESSION)
    assert resp.status_code == 200


async def test_valid_csrf_without_origin_rejected_by_middleware(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    async with make_extra_client(app, origin=None, cookies=client.cookies) as bare:
        resp = await bare.post(LOGOUT, headers=csrf_headers(owner))
        assert_problem(resp, status=403, code="request.bad_origin")
    resp = await client.get(SESSION)
    assert resp.status_code == 200


# --- CSRF tokens are per session -------------------------------------------------------


async def test_csrf_token_is_bound_to_its_session(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    async with make_extra_client(app) as second:
        other_csrf = await login_mfa_pending(second, owner)
        assert other_csrf != owner.csrf_token
        # The second session's token on the first session's request...
        resp = await client.post(LOGOUT, headers={CSRF_HEADER: other_csrf})
        assert_problem(resp, status=403, code="csrf.invalid")
        # ...and the first session's token on the second session's request.
        resp = await second.post(LOGOUT, headers=csrf_headers(owner))
        assert_problem(resp, status=403, code="csrf.invalid")
        # Both sessions survived the mismatched attempts.
        assert (await second.get(SESSION)).status_code == 200
    resp = await client.get(SESSION)
    assert resp.status_code == 200
    assert resp.json()["session_state"] == "active"


async def test_mfa_pending_session_can_logout_with_its_csrf(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    async with make_extra_client(app) as second:
        pending_csrf = await login_mfa_pending(second, owner)
        resp = await second.post(LOGOUT, headers={CSRF_HEADER: pending_csrf})
        assert resp.status_code == 204, resp.text
        resp = await second.get(SESSION)
        assert_problem(resp, status=401, code="auth.required")
    # The owner's original active session is untouched.
    resp = await client.get(SESSION)
    assert resp.status_code == 200
    assert resp.json()["session_state"] == "active"


# --- Safe methods need no CSRF token ---------------------------------------------------


async def test_get_requests_do_not_require_csrf(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    # The shared client never sends X-CSRF-Token by default.
    resp = await client.get(SESSION)
    assert resp.status_code == 200
    # Stateless HMAC token: identical on every read of the same session.
    assert resp.json()["csrf_token"] == owner.csrf_token
    resp = await client.get(SESSIONS)
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
