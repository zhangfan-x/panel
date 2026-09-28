"""Docker container detail: one-shot stats + exec WebSocket (docs §3.4/§4.5/§5.3).

The ``docker.stats`` snapshot is validated at the pure-compute level (a synthetic
engine stats dict fed to :func:`dockersvc._compute_stats`, so no daemon is
needed) plus helper-side argument validation for ``docker.stats`` /
``docker.exec_resize``. The API layer covers the auth gate on ``/stats`` and the
503 daemon-down degradation. The ``WS .../exec`` endpoint is checked for its
manual Origin/session rejection (code 1008) — mirroring the terminal WS tests,
the real daemon socket is never opened because rejection happens before accept.
The suite stays green whether or not a docker daemon runs.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import API, OwnerContext, SettingsFactory, naive_utcnow
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_helper import running_helper

from mypanel.app import create_app
from mypanel.auth.deps import COOKIE_NAME
from mypanel.config import Settings
from mypanel.db import run_migrations
from mypanel.errors import HelperActionError
from mypanel.helper.actions import dockersvc
from mypanel.ipc.client import HelperClient
from mypanel.security import generate_token, hash_token

# Importing the actions package registers every docker.* action.
importlib.import_module("mypanel.helper.actions")

DOCKER = f"{API}/docker"
UNKNOWN = "mypanel-test-no-such-object"
GOOD_ORIGIN = "http://testserver"
STATS_FIELDS = {
    "read",
    "cpu_percent",
    "online_cpus",
    "mem_used",
    "mem_limit",
    "mem_percent",
    "net_rx",
    "net_tx",
    "blk_read",
    "blk_write",
    "pids",
}


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


# ---------------------------------------------------------------------------
# docker.stats: pure compute (no daemon)
# ---------------------------------------------------------------------------


def _sample_stats() -> dict[str, Any]:
    """A realistic docker one-shot stats dict with a known-good answer."""
    return {
        "read": "2026-07-22T10:00:00.000000000Z",
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200_000_000, "percpu_usage": [1, 2, 3, 4]},
            "system_cpu_usage": 2_000_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 1_000_000_000,
        },
        "memory_stats": {
            "usage": 200 * 1024 * 1024,
            "limit": 1024 * 1024 * 1024,
            "stats": {"inactive_file": 50 * 1024 * 1024},
        },
        "networks": {
            "eth0": {"rx_bytes": 1000, "tx_bytes": 2000},
            "eth1": {"rx_bytes": 5, "tx_bytes": 7},
        },
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": 4096},
                {"op": "Write", "value": 8192},
                {"op": "Async", "value": 9999},
            ]
        },
        "pids_stats": {"current": 3},
    }


class TestComputeStats:
    def test_flat_shape(self) -> None:
        result = dockersvc._compute_stats(_sample_stats())
        assert set(result) == STATS_FIELDS

    def test_cpu_percent_uses_engine_formula(self) -> None:
        # cpu_delta/ system_delta * online * 100 = 0.1 * 4 * 100 = 40.0
        assert dockersvc._compute_stats(_sample_stats())["cpu_percent"] == 40.0

    def test_memory_subtracts_inactive_file_cache(self) -> None:
        result = dockersvc._compute_stats(_sample_stats())
        assert result["mem_used"] == 150 * 1024 * 1024
        assert result["mem_limit"] == 1024 * 1024 * 1024
        assert result["mem_percent"] == round(150 / 1024 * 100, 2)

    def test_network_and_block_io_totals(self) -> None:
        result = dockersvc._compute_stats(_sample_stats())
        assert result["net_rx"] == 1005
        assert result["net_tx"] == 2007
        # only Read/Write ops count; Async is ignored.
        assert result["blk_read"] == 4096
        assert result["blk_write"] == 8192
        assert result["pids"] == 3

    def test_cgroup_v1_inactive_file_fallback(self) -> None:
        raw = _sample_stats()
        raw["memory_stats"]["stats"] = {"total_inactive_file": 20 * 1024 * 1024}
        result = dockersvc._compute_stats(raw)
        assert result["mem_used"] == (200 - 20) * 1024 * 1024

    def test_online_cpus_falls_back_to_percpu_length(self) -> None:
        raw = _sample_stats()
        del raw["cpu_stats"]["online_cpus"]
        # percpu_usage has 4 entries -> still 40.0.
        assert dockersvc._compute_stats(raw)["online_cpus"] == 4
        assert dockersvc._compute_stats(raw)["cpu_percent"] == 40.0

    def test_stopped_container_yields_zeros_without_error(self) -> None:
        # Empty cpu/precpu/memory (a stopped container) must not divide by zero.
        result = dockersvc._compute_stats(
            {"read": "", "cpu_stats": {}, "precpu_stats": {}, "memory_stats": {}}
        )
        assert result["cpu_percent"] == 0.0
        assert result["mem_percent"] == 0.0
        assert result["mem_used"] == 0
        assert result["net_rx"] == 0

    def test_missing_sections_are_tolerated(self) -> None:
        # A wholly empty payload still produces the flat contract with zeros.
        result = dockersvc._compute_stats({})
        assert set(result) == STATS_FIELDS
        assert result["read"] == ""
        assert result["pids"] == 0


# ---------------------------------------------------------------------------
# helper: argument validation (rejected before any daemon contact)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def helper_socket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("docker-detail-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture
def settings(make_settings: SettingsFactory, helper_socket: Path) -> Settings:
    return make_settings(paths={"helper_socket": helper_socket})


@pytest.fixture
def broken_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force docker.* actions to see an unreachable daemon (in-process helper)."""
    monkeypatch.setattr(dockersvc, "_docker_client", None)
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/mypanel-docker.sock")


class TestHelperValidation:
    @pytest.mark.parametrize(
        "bad_id",
        ["../etc", "a b", "-lead", "", "a/b", "a;b", "a\x00b"],
        ids=["dotdot", "space", "dash", "empty", "slash", "semicolon", "nul"],
    )
    def test_stats_ids_are_validated(self, helper: HelperClient, bad_id: str) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.stats", {"id": bad_id})
        assert excinfo.value.helper_code == "helper.invalid_args"

    def test_exec_resize_rejects_bad_exec_id(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.exec_resize", {"exec_id": "../oops", "height": 24, "width": 80})
        assert excinfo.value.helper_code == "helper.invalid_args"

    @pytest.mark.parametrize(
        ("height", "width"),
        [(0, 80), (24, 0), (5000, 80), (24, 5000)],
        ids=["zero-h", "zero-w", "huge-h", "huge-w"],
    )
    def test_exec_resize_dimension_bounds(
        self, helper: HelperClient, height: int, width: int
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call(
                "docker.exec_resize",
                {"exec_id": "a" * 64, "height": height, "width": width},
            )
        assert excinfo.value.helper_code == "helper.invalid_args"


class TestHelperDaemonDown:
    def test_stats_maps_to_docker_unavailable(
        self, helper: HelperClient, broken_docker: None
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.stats", {"id": "abc"})
        assert excinfo.value.helper_code == "helper.docker_unavailable"


# ---------------------------------------------------------------------------
# API: auth gate + daemon-down degradation
# ---------------------------------------------------------------------------


async def test_stats_requires_auth(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get(f"{DOCKER}/containers/{UNKNOWN}/stats"), 401, "auth.required")


async def test_stats_bad_id_is_validation_error(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    assert_problem(await client.get(f"{DOCKER}/containers/bad id/stats"), 422, "request.validation")


async def test_stats_daemon_down_is_503(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, broken_docker: None
) -> None:
    resp = await client.get(f"{DOCKER}/containers/{UNKNOWN}/stats")
    assert_problem(resp, 503, "docker.unavailable")


# ---------------------------------------------------------------------------
# exec WebSocket: manual Origin/session rejection (before accept, no daemon)
# ---------------------------------------------------------------------------

EXEC_WS_PATH = f"{API}/docker/containers/web/exec"


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


def test_exec_ws_rejects_bad_origin(make_settings: SettingsFactory) -> None:
    app, raw_token = _build_ws_app(make_settings)
    headers = {"Origin": "http://evil.example", "Cookie": f"{COOKIE_NAME}={raw_token}"}
    with TestClient(app) as tc:
        connect = tc.websocket_connect(EXEC_WS_PATH, headers=headers)
        with pytest.raises(WebSocketDisconnect) as exc, connect:
            pass
        assert exc.value.code == 1008


def test_exec_ws_rejects_missing_origin(make_settings: SettingsFactory) -> None:
    app, raw_token = _build_ws_app(make_settings)
    headers = {"Cookie": f"{COOKIE_NAME}={raw_token}"}
    with TestClient(app) as tc:
        connect = tc.websocket_connect(EXEC_WS_PATH, headers=headers)
        with pytest.raises(WebSocketDisconnect) as exc, connect:
            pass
        assert exc.value.code == 1008


def test_exec_ws_rejects_missing_cookie(make_settings: SettingsFactory) -> None:
    app, _raw_token = _build_ws_app(make_settings)
    headers = {"Origin": GOOD_ORIGIN}
    with TestClient(app) as tc:
        connect = tc.websocket_connect(EXEC_WS_PATH, headers=headers)
        with pytest.raises(WebSocketDisconnect) as exc, connect:
            pass
        assert exc.value.code == 1008


def test_exec_ws_rejects_bad_container_id(make_settings: SettingsFactory) -> None:
    # A path id that fails NAME_PATTERN is rejected before accept, like a bad origin.
    app, raw_token = _build_ws_app(make_settings)
    headers = {"Origin": GOOD_ORIGIN, "Cookie": f"{COOKIE_NAME}={raw_token}"}
    with TestClient(app) as tc:
        connect = tc.websocket_connect(f"{API}/docker/containers/-bad/exec", headers=headers)
        with pytest.raises(WebSocketDisconnect) as exc, connect:
            pass
        assert exc.value.code == 1008
