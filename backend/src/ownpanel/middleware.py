"""Pure ASGI middleware: request IDs, security response headers, Host/Origin enforcement.

Deliberately not BaseHTTPMiddleware subclasses: BaseHTTPMiddleware buffers responses
and would break SSE streaming later. Only ``http`` scopes are processed; everything
else passes through untouched.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable
from typing import Any

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ownpanel.config import Settings
from ownpanel.errors import AppError, BadHostError, BadOriginError

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", _CSP),
)


def _header_value(scope: Scope, name: bytes) -> str | None:
    """First value of header `name` (lowercase bytes) from the raw ASGI header list."""
    raw_headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or ()
    for key, value in raw_headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _send_problem(scope: Scope, send: Send, error: AppError) -> None:
    """Send an RFC 9457 problem+json response for a middleware-level rejection."""
    request_id = (scope.get("state") or {}).get("request_id")
    body = json.dumps(error.problem(request_id), separators=(",", ":")).encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/problem+json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if request_id is not None:
        headers.append((b"x-request-id", str(request_id).encode("latin-1")))
    await send({"type": "http.response.start", "status": error.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestContextMiddleware:
    """Assign a request id: scope state, structlog contextvars, X-Request-Id header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = "req_" + secrets.token_hex(6)
        # Starlette's Request.state is a view over scope["state"]; the key may not exist
        # yet because this middleware sits outside the Starlette application.
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message)["X-Request-Id"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


class SecurityHeadersMiddleware:
    """Set the fixed security header set on every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HostOriginMiddleware:
    """Reject requests with a bad Host (all methods) or bad Origin (state-changing methods).

    Forwarded / X-Forwarded-* are never consulted: the panel terminates TLS itself and
    is not meant to sit behind a reverse proxy. Host matching is an exact string match
    including the port; both sides are lowercased (header field values are
    case-insensitive per RFC 9110).
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._allowed_hosts = {h.lower() for h in settings.allowed_hosts()}
        self._allowed_origins = {o.lower() for o in settings.allowed_origins()}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = _header_value(scope, b"host")
        if host is None or host.strip().lower() not in self._allowed_hosts:
            await _send_problem(scope, send, BadHostError())
            return
        if scope["method"].upper() in _UNSAFE_METHODS:
            origin = _header_value(scope, b"origin")
            if origin is None or origin.strip().lower() not in self._allowed_origins:
                await _send_problem(scope, send, BadOriginError())
                return
        await self.app(scope, receive, send)
