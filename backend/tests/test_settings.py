"""Settings read-only API: GET /settings/system and GET /settings/profile.

Covers docs/code-plan-m2-m5.md §4.9: exact response shapes, values mirroring the
effective ``Settings``, and — critically — no secret material in the system view.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from conftest import API, OWNER_USERNAME, OwnerContext

import ownpanel
from ownpanel.config import Settings

SYSTEM = f"{API}/settings/system"
PROFILE = f"{API}/settings/profile"


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


async def test_settings_require_auth(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get(SYSTEM), 401, "auth.required")
    assert_problem(await client.get(PROFILE), 401, "auth.required")


async def test_system_settings_shape(
    client: httpx.AsyncClient, owner: OwnerContext, settings: Settings
) -> None:
    resp = await client.get(SYSTEM)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body) == {"server", "limits", "docker", "version"}
    assert body["server"] == {
        "host": settings.server.host,
        "port": settings.server.port,
        "dev_http": settings.server.dev_http,
        "tls_cert": str(settings.server.tls_cert),
        "tls_key": str(settings.server.tls_key),
    }
    # Every LimitsConfig field, verbatim (ints survive the JSON round-trip).
    assert body["limits"] == settings.limits.model_dump()
    assert body["limits"]["session_idle_minutes"] == 30
    assert body["limits"]["audit_retention_days"] == 90
    assert body["docker"] == {"compose_root": str(settings.docker.compose_root)}
    assert body["version"] == ownpanel.__version__


async def test_system_settings_leak_no_secrets(
    client: httpx.AsyncClient, owner: OwnerContext, settings: Settings
) -> None:
    resp = await client.get(SYSTEM)
    assert resp.status_code == 200
    text = resp.text.lower()
    assert "secret" not in text
    # The raw key material must never appear in any encoding of itself.
    assert settings.secret_key.hex() not in text
    assert str(settings.paths.data_dir).lower() not in text


async def test_profile_shape(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.get(PROFILE)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body) == {"username", "created_at", "password_changed_at"}
    assert body["username"] == OWNER_USERNAME
    created_at = datetime.fromisoformat(body["created_at"])
    password_changed_at = datetime.fromisoformat(body["password_changed_at"])
    assert created_at.tzinfo is not None
    assert password_changed_at.tzinfo is not None
    assert password_changed_at >= created_at
