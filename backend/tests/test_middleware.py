"""Middleware behavior over the full ASGI stack (docs/code-plan.md sections 5.3 and 6).

Security headers, X-Request-Id, Host/Origin enforcement, Forwarded/X-Forwarded-*
immunity. The shared ``client`` sends Host 127.0.0.1:25443 and a matching Origin by
default; ``bare_client`` below sends no Origin at all.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from conftest import API, BASE_URL

from mypanel.middleware import HostOriginMiddleware

HEALTH = f"{API}/health"
LOGIN = f"{API}/auth/login"

PROBLEM_TYPE = "application/problem+json"

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CSP,
}

REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{12}$")

FORWARDED_HEADERS = {
    "X-Forwarded-For": "6.6.6.6",
    "X-Forwarded-Host": "evil.example",
    "X-Forwarded-Proto": "https",
    "Forwarded": "for=6.6.6.6;host=evil.example;proto=https",
}


@pytest.fixture
async def bare_client(app, client):
    """Client without any default Origin header (``client`` keeps the lifespan open)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as c:
        yield c


def _assert_security_headers(resp: httpx.Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers.get(name) == value, f"header {name} wrong or missing"


# --- security headers & request id -------------------------------------------------


async def test_security_headers_exact_on_success(client):
    resp = await client.get(HEALTH)
    assert resp.status_code == 200
    _assert_security_headers(resp)
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str)


async def test_request_id_present_and_unique_per_request(client):
    first = await client.get(HEALTH)
    second = await client.get(HEALTH)
    for resp in (first, second):
        assert REQUEST_ID_RE.fullmatch(resp.headers["X-Request-Id"])
    assert first.headers["X-Request-Id"] != second.headers["X-Request-Id"]


async def test_rejection_still_carries_security_headers_and_request_id(client):
    resp = await client.get(HEALTH, headers={"Host": "evil.example"})
    assert resp.status_code == 400
    _assert_security_headers(resp)
    request_id = resp.headers["X-Request-Id"]
    assert REQUEST_ID_RE.fullmatch(request_id)
    assert resp.json()["request_id"] == request_id


async def test_unhandled_500_still_carries_security_headers_and_request_id(app):
    """A route that raises an unexpected error is served by Starlette's outermost
    ServerErrorMiddleware, bypassing the header middleware; the problem response must
    still carry the security headers and X-Request-Id (code-plan §6)."""

    @app.get("/api/v1/_boom")
    async def _boom() -> None:
        raise RuntimeError("boom")

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers={"Origin": BASE_URL}
        ) as c:
            resp = await c.get("/api/v1/_boom")

    assert resp.status_code == 500
    assert resp.headers["Content-Type"] == PROBLEM_TYPE
    _assert_security_headers(resp)
    request_id = resp.headers["X-Request-Id"]
    assert REQUEST_ID_RE.fullmatch(request_id)
    assert resp.json()["request_id"] == request_id


# --- Host enforcement ---------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:25443",
        "127.0.0.1:9999",  # wrong port
        "localhost",  # missing port
        "127.0.0.1",  # missing port
    ],
)
async def test_forged_host_rejected(client, host):
    resp = await client.get(HEALTH, headers={"Host": host})
    assert resp.status_code == 400
    assert resp.headers["Content-Type"] == PROBLEM_TYPE
    body = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == 400
    assert body["code"] == "request.bad_host"
    assert body["title"] == "Host Not Allowed"


@pytest.mark.parametrize("host", ["127.0.0.1:25443", "localhost:25443", "LOCALHOST:25443"])
async def test_allowed_host_forms_accepted(client, host):
    resp = await client.get(HEALTH, headers={"Host": host})
    assert resp.status_code == 200


async def test_missing_host_header_rejected_at_asgi_level(settings):
    """httpx always sends Host, so exercise the middleware directly without one."""
    inner = _Recorder()
    middleware = HostOriginMiddleware(inner, settings)
    sent = await _call(middleware, {"type": "http", "method": "GET", "path": "/", "headers": []})
    assert inner.scopes == []  # never reached the app
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400
    assert (b"content-type", b"application/problem+json") in sent[0]["headers"]
    assert json.loads(sent[1]["body"])["code"] == "request.bad_host"


async def test_non_http_scopes_pass_through_untouched(settings):
    inner = _Recorder()
    middleware = HostOriginMiddleware(inner, settings)
    scope = {"type": "lifespan"}
    await _call(middleware, scope)
    assert inner.scopes == [scope]


# --- Origin enforcement --------------------------------------------------------------


async def test_post_without_origin_rejected(bare_client):
    resp = await bare_client.post(LOGIN, json={"username": "u", "password": "p"})
    assert "origin" not in resp.request.headers
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "request.bad_origin"
    assert body["title"] == "Origin Not Allowed"


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:25443",  # https form is wrong under dev_http
        "HTTPS://127.0.0.1:25443",  # case variant of the wrong scheme
        "http://127.0.0.1:9999",  # wrong port
        "http://127.0.0.1",  # missing port
        "http://evil.example",  # foreign host
        "null",  # opaque origin
        "",  # empty header value
    ],
)
async def test_bad_origin_rejected(client, origin):
    resp = await client.post(
        LOGIN, json={"username": "u", "password": "p"}, headers={"Origin": origin}
    )
    assert resp.status_code == 403
    assert resp.headers["Content-Type"] == PROBLEM_TYPE
    assert resp.json()["code"] == "request.bad_origin"


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:25443",
        "http://localhost:25443",
        "HTTP://127.0.0.1:25443",  # scheme/host comparison is case-insensitive
        "http://localhost:5173",  # Vite dev-server origin allowed under dev_http
    ],
)
async def test_valid_origin_reaches_business_layer(client, origin):
    # An empty password fails request validation: proof the middleware let it through.
    resp = await client.post(
        LOGIN, json={"username": "u", "password": ""}, headers={"Origin": origin}
    )
    assert resp.status_code == 422
    assert resp.headers["Content-Type"] == PROBLEM_TYPE
    assert resp.json()["code"] == "request.validation"


async def test_get_does_not_require_origin(bare_client):
    resp = await bare_client.get(HEALTH)
    assert "origin" not in resp.request.headers
    assert resp.status_code == 200


# --- Forwarded / X-Forwarded-* are ignored -------------------------------------------


async def test_forwarded_headers_never_influence_host_check(client):
    ok = await client.get(HEALTH, headers=FORWARDED_HEADERS)
    assert ok.status_code == 200

    spoofed = await client.get(
        HEALTH,
        headers={**FORWARDED_HEADERS, "Host": "evil.example", "X-Forwarded-Host": BASE_URL},
    )
    assert spoofed.status_code == 400
    assert spoofed.json()["code"] == "request.bad_host"


async def test_forwarded_headers_never_influence_origin_check(client):
    passed = await client.post(
        LOGIN, json={"username": "u", "password": ""}, headers=FORWARDED_HEADERS
    )
    assert passed.status_code == 422  # the client's genuine Origin still decides

    denied = await client.post(
        LOGIN,
        json={"username": "u", "password": "p"},
        headers={**FORWARDED_HEADERS, "Origin": "https://evil.example"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "request.bad_origin"


# --- app-level defaults ---------------------------------------------------------------


async def test_unknown_api_path_is_fastapi_default_404(client):
    resp = await client.get(f"{API}/definitely-not-here")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    _assert_security_headers(resp)
    assert REQUEST_ID_RE.fullmatch(resp.headers["X-Request-Id"])


# --- ASGI helpers ---------------------------------------------------------------------


class _Recorder:
    """Minimal inner ASGI app that records the scopes it receives."""

    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.scopes.append(scope)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


async def _call(middleware, scope):
    sent: list[dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    await middleware(scope, receive, send)
    return sent
