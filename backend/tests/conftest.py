"""Shared pytest infrastructure for the ownpanel backend test suite (team-frozen).

pytest inserts ``tests/`` into ``sys.path`` (there is no ``__init__.py``), so besides
the fixtures below, the plain helpers are importable from any test module:

    from conftest import API, BASE_URL, ORIGIN, OWNER_PASSWORD, OWNER_USERNAME
    from conftest import OwnerContext, csrf_headers, db_execute, db_scalar, naive_utcnow

Ground rules baked into these fixtures (docs/code-plan.md sections 5, 6 and 9):

* Every test gets isolated ``Settings`` rooted under ``tmp_path``: its own SQLite
  database, secret key and helper/terminal socket paths. ``create_app()`` overwrites
  the process-wide settings singleton, so never rely on
  ``ownpanel.config.get_settings()`` when more than one app exists in a test.
* ``httpx.ASGITransport`` does not run the lifespan. The ``client`` fixture wraps
  ``app.router.lifespan_context(app)``, which runs the migrations and creates
  ``app.state.engine`` / ``app.state.sessionmaker``. Anything that touches
  ``app.state.sessionmaker`` (``owner``, ``db_execute``/``db_scalar`` calls, ...)
  must therefore depend on ``client`` directly or transitively.
* SQLite returns naive UTC datetimes. Backdate rows by writing naive UTC values
  (``naive_utcnow() - timedelta(...)``) and re-attach ``tzinfo=UTC`` before
  comparing anything read back from the database.
* To assert 500 responses, build a dedicated transport with
  ``httpx.ASGITransport(app=app, raise_app_exceptions=False)``; the shared
  ``client`` keeps the default so unexpected server errors fail tests loudly.
* Need custom settings (limits, ports, hosts)? Override the ``settings`` fixture in
  your own module using ``make_settings(...)`` — the ``app`` and ``client`` fixtures
  pick the override up automatically.
* Rate-limit tests should use per-test usernames/keys: limiter state lives on the
  app instance, and rejected acquires also count.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from ownpanel.app import create_app
from ownpanel.auth.service import AuthService
from ownpanel.config import Settings

API = "/api/v1"
BASE_URL = "http://127.0.0.1:25443"
ORIGIN = "http://127.0.0.1:25443"

OWNER_USERNAME = "owner"
# Satisfies the policy: >= 12 characters, several character classes.
OWNER_PASSWORD = "correct-horse-battery-9!"

SettingsFactory = Callable[..., Settings]


@dataclass
class OwnerContext:
    """Credentials and tokens of the bootstrapped owner (see the ``owner`` fixture)."""

    username: str
    password: str
    csrf_token: str


def csrf_headers(ctx: OwnerContext) -> dict[str, str]:
    """Header dict carrying the owner's CSRF token for state-changing requests."""
    return {"X-CSRF-Token": ctx.csrf_token}


def naive_utcnow() -> datetime:
    """Current UTC time as a naive datetime (SQLite storage format), for backdating."""
    return datetime.now(tz=UTC).replace(tzinfo=None)


async def db_execute(app: FastAPI, stmt: Any) -> Any:
    """Execute one statement on the app's sessionmaker, commit, return the Result.

    Intended for UPDATE/DELETE statements (e.g. backdating session timestamps).
    Requires an active lifespan, i.e. the ``client`` fixture must be in use.
    """
    async with app.state.sessionmaker() as db:
        result = await db.execute(stmt)
        await db.commit()
        return result


async def db_scalar(app: FastAPI, stmt: Any) -> Any:
    """Fetch a single scalar via the app's sessionmaker (read-only convenience)."""
    async with app.state.sessionmaker() as db:
        return await db.scalar(stmt)


@pytest.fixture
def make_settings(tmp_path: Path) -> SettingsFactory:
    """Factory for isolated test ``Settings``.

    Each call without ``base_dir`` gets a fresh sub-directory of ``tmp_path`` (own
    data_dir, database and secret key). Keyword arguments:

    * ``host`` / ``port`` / ``dev_http`` — common server knobs. Defaults match the
      shared ``client``: dev_http=true on 127.0.0.1:25443 (see BASE_URL/ORIGIN).
    * ``server`` / ``paths`` / ``limits`` — dicts merged into the corresponding
      config section, e.g. ``limits={"session_idle_minutes": 1}``.
    * ``base_dir`` — explicit directory, to share a data_dir between instances.
    """
    counter = itertools.count()

    def make(
        *,
        base_dir: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 25443,
        dev_http: bool = True,
        server: dict[str, Any] | None = None,
        paths: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
    ) -> Settings:
        base = tmp_path / f"cfg{next(counter)}" if base_dir is None else base_dir
        server_cfg: dict[str, Any] = {"host": host, "port": port, "dev_http": dev_http}
        server_cfg.update(server or {})
        paths_cfg: dict[str, Any] = {
            "data_dir": base / "data",
            "helper_socket": base / "helper.sock",
            "terminal_socket": base / "terminal.sock",
        }
        paths_cfg.update(paths or {})
        data: dict[str, Any] = {"server": server_cfg, "paths": paths_cfg}
        if limits is not None:
            data["limits"] = limits
        return Settings.model_validate(data)

    return make


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    """Default test settings: dev_http on 127.0.0.1:25443, everything under tmp_path."""
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Application built from the test settings. The lifespan is NOT started here."""
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client over ASGI with the app lifespan running.

    Sends Host ``127.0.0.1:25443`` (from ``base_url``) plus a matching ``Origin``
    header by default, so state-changing requests pass the Host/Origin middleware.
    Session cookies accumulate on the client instance across requests.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers={"Origin": ORIGIN}
        ) as http_client:
            yield http_client


@pytest.fixture
async def owner(app: FastAPI, client: httpx.AsyncClient) -> OwnerContext:
    """Bootstrapped owner with an ACTIVE session held by ``client``.

    Flow: issue a bootstrap token at service level -> POST /bootstrap/owner
    (session is active immediately) -> GET /auth/session for the CSRF token.
    The session cookie ends up in ``client.cookies``; pass
    ``csrf_headers(owner)`` on state-changing requests.
    """
    async with app.state.sessionmaker() as db:
        token = await AuthService(db, app.state.settings).issue_bootstrap_token()

    resp = await client.post(
        f"{API}/bootstrap/owner",
        json={"token": token, "username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )
    assert resp.status_code == 200, f"bootstrap/owner failed: {resp.status_code} {resp.text}"
    assert resp.json()["session_state"] == "active"

    resp = await client.get(f"{API}/auth/session")
    assert resp.status_code == 200, f"auth/session failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["session_state"] == "active"
    return OwnerContext(
        username=OWNER_USERNAME,
        password=OWNER_PASSWORD,
        csrf_token=body["csrf_token"],
    )
