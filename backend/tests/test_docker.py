"""Docker domain tests (docs/code-plan-m2-m5.md §3.4 and §4.5).

Helper level: argument validation of the ``docker.*`` actions (rejected by the
args models before any daemon contact), forced daemon-down degradation via a
bogus ``DOCKER_HOST``, and — when a local daemon is reachable — a light real
smoke (info/list shapes, not-found mapping). API level: auth/CSRF/re-auth
gates, 502 helper-down and 503 daemon-down degradation, semantic error mapping
and audit rows. The suite stays green whether or not a docker daemon runs.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from conftest import API, OwnerContext, SettingsFactory, csrf_headers, db_scalar
from fastapi import FastAPI
from sqlalchemy import select
from test_helper import running_helper

from mypanel.config import Settings
from mypanel.db.models import AuditLog
from mypanel.errors import HelperActionError
from mypanel.helper.actions import dockersvc
from mypanel.ipc.client import HelperClient

# Importing the actions package registers every docker.* action.
importlib.import_module("mypanel.helper.actions")

DOCKER = f"{API}/docker"
UNKNOWN = "mypanel-test-no-such-object"

CONTAINER_ROW_FIELDS = {"id", "name", "image", "state", "status", "ports", "created", "labels"}
IMAGE_ROW_FIELDS = {"id", "tags", "size", "created"}
NETWORK_ROW_FIELDS = {"id", "name", "driver", "scope", "internal", "created"}
VOLUME_ROW_FIELDS = {"name", "driver", "mountpoint", "created", "scope", "labels"}


def _daemon_available() -> bool:
    """Probe the local docker daemon once at collection time."""
    import docker

    try:
        client = docker.from_env()
        try:
            client.ping()
        finally:
            client.close()
        return True
    except Exception:
        return False


DAEMON_UP = _daemon_available()
needs_daemon = pytest.mark.skipif(not DAEMON_UP, reason="local docker daemon is not available")


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


@pytest.fixture(scope="module")
def helper_socket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-wide helper socket path, shared by the server and the app settings."""
    return tmp_path_factory.mktemp("docker-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    """Client for a module-wide helper server that allows the current UID."""
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture
def settings(make_settings: SettingsFactory, helper_socket: Path) -> Settings:
    """App settings pointing at the module helper socket."""
    return make_settings(paths={"helper_socket": helper_socket})


@pytest.fixture
def broken_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every docker.* action to see an unreachable daemon.

    The helper server runs in-process, so clearing the cached client and
    pointing ``DOCKER_HOST`` at a dead socket affects its handler threads too.
    """
    monkeypatch.setattr(dockersvc, "_docker_client", None)
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/mypanel-docker.sock")


# ---------------------------------------------------------------------------
# helper: argument validation (pydantic rejects before any daemon contact)
# ---------------------------------------------------------------------------


class TestHelperValidation:
    def test_container_action_rejects_unknown_verb(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.container_action", {"id": "abc", "action": "explode"})
        assert excinfo.value.helper_code == "helper.invalid_args"

    @pytest.mark.parametrize(
        "bad_id",
        ["../etc", "a b", "-lead", "", "a/b", "a;b", "a\x00b"],
        ids=["dotdot", "space", "dash", "empty", "slash", "semicolon", "nul"],
    )
    def test_container_ids_are_validated(self, helper: HelperClient, bad_id: str) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.container_action", {"id": bad_id, "action": "start"})
        assert excinfo.value.helper_code == "helper.invalid_args"

    @pytest.mark.parametrize("tail", [0, 100_000], ids=["zero", "huge"])
    def test_logs_tail_bounds(self, helper: HelperClient, tail: int) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.logs", {"id": "abc", "tail": tail})
        assert excinfo.value.helper_code == "helper.invalid_args"

    @pytest.mark.parametrize(
        ("repository", "tag"),
        [("nginx; rm -rf /", "latest"), ("nginx", "v1 beta"), ("", "latest")],
        ids=["shell-repo", "space-tag", "empty-repo"],
    )
    def test_image_pull_reference_is_validated(
        self, helper: HelperClient, repository: str, tag: str
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.image_pull", {"repository": repository, "tag": tag})
        assert excinfo.value.helper_code == "helper.invalid_args"

    def test_exec_requires_a_command(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.exec_create", {"id": "abc", "cmd": []})
        assert excinfo.value.helper_code == "helper.invalid_args"

    def test_network_driver_is_validated(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.network_create", {"name": "n1", "driver": "evil"})
        assert excinfo.value.helper_code == "helper.invalid_args"

    def test_volume_name_is_validated(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.volume_create", {"name": "../vol"})
        assert excinfo.value.helper_code == "helper.invalid_args"


def test_snapshot_budget_keeps_newest_lines() -> None:
    lines = [f"line-{i:04d}" for i in range(100)]
    kept, truncated = dockersvc._tail_lines_within_budget(lines, budget=140)
    assert truncated
    assert 0 < len(kept) < 100
    assert kept == lines[-len(kept) :]  # newest survive, order preserved

    kept_all, truncated_all = dockersvc._tail_lines_within_budget(lines, budget=10_000)
    assert kept_all == lines
    assert not truncated_all


# ---------------------------------------------------------------------------
# helper: daemon down (forced deterministically)
# ---------------------------------------------------------------------------


class TestHelperDaemonDown:
    def test_info_reports_unavailable_instead_of_failing(
        self, helper: HelperClient, broken_docker: None
    ) -> None:
        assert helper.call("docker.info") == {"available": False}

    def test_actions_map_to_docker_unavailable(
        self, helper: HelperClient, broken_docker: None
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.container_list", {"all": True})
        assert excinfo.value.helper_code == "helper.docker_unavailable"

    def test_stats_all_maps_to_docker_unavailable(
        self, helper: HelperClient, broken_docker: None
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.stats_all")
        assert excinfo.value.helper_code == "helper.docker_unavailable"


# ---------------------------------------------------------------------------
# helper: real daemon smoke (skipped when no daemon is reachable)
# ---------------------------------------------------------------------------


@needs_daemon
class TestHelperDaemonSmoke:
    def test_info_reports_available_daemon(self, helper: HelperClient) -> None:
        info = helper.call("docker.info")
        assert info["available"] is True
        assert info["version"]
        assert isinstance(info["containers"], int)
        assert info["running"] <= info["containers"]

    def test_container_list_shape(self, helper: HelperClient) -> None:
        result = helper.call("docker.container_list", {"all": True})
        for row in result["containers"]:
            assert set(row) == CONTAINER_ROW_FIELDS
            assert row["id"] and row["name"]

    def test_unknown_container_inspect_is_not_found(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.container_inspect", {"id": UNKNOWN})
        assert excinfo.value.helper_code == "helper.not_found"

    def test_unknown_container_action_is_not_found(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.container_action", {"id": UNKNOWN, "action": "start"})
        assert excinfo.value.helper_code == "helper.not_found"

    def test_unknown_container_logs_snapshot_and_follow(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("docker.logs", {"id": UNKNOWN, "tail": 10})
        assert excinfo.value.helper_code == "helper.not_found"
        with pytest.raises(HelperActionError) as excinfo:
            helper.call_with_fds("docker.logs", {"id": UNKNOWN, "tail": 10, "follow": True})
        assert excinfo.value.helper_code == "helper.not_found"

    def test_stats_all_shape(self, helper: HelperClient) -> None:
        # Only running containers appear; each snapshot is the flat
        # _compute_stats shape keyed by the full container id.
        result = helper.call("docker.stats_all")
        assert isinstance(result["stats"], dict)
        for container_id, snapshot in result["stats"].items():
            assert isinstance(container_id, str) and container_id
            assert isinstance(snapshot["cpu_percent"], int | float)
            assert isinstance(snapshot["mem_used"], int)

    def test_image_network_volume_list_shapes(self, helper: HelperClient) -> None:
        for row in helper.call("docker.image_list")["images"]:
            assert set(row) == IMAGE_ROW_FIELDS
        networks = helper.call("docker.network_list")["networks"]
        assert any(n["name"] == "bridge" for n in networks)  # engine built-in
        for row in networks:
            assert set(row) == NETWORK_ROW_FIELDS
        for row in helper.call("docker.volume_list")["volumes"]:
            assert set(row) == VOLUME_ROW_FIELDS


# ---------------------------------------------------------------------------
# API: auth, CSRF and re-auth gates (no daemon required)
# ---------------------------------------------------------------------------


async def test_endpoints_require_auth(client: httpx.AsyncClient) -> None:
    for path in (
        f"{DOCKER}/info",
        f"{DOCKER}/containers",
        f"{DOCKER}/stats",
        f"{DOCKER}/images",
    ):
        assert_problem(await client.get(path), 401, "auth.required")


async def test_container_action_without_csrf_is_rejected(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(f"{DOCKER}/containers/{UNKNOWN}/start")
    assert_problem(resp, 403, "csrf.invalid")


async def test_mutations_without_csrf_are_rejected(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(f"{DOCKER}/images/pull", json={"repository": "nginx"})
    assert_problem(resp, 403, "csrf.invalid")
    resp = await client.post(f"{DOCKER}/networks", json={"name": "n1"})
    assert_problem(resp, 403, "csrf.invalid")
    resp = await client.delete(f"{DOCKER}/volumes/v1")
    assert_problem(resp, 403, "csrf.invalid")


async def test_container_remove_requires_recent_reauth(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    # Fresh sessions have reauth_at NULL; CSRF passes, the re-auth gate trips
    # before any helper call, and a denied audit row is written.
    resp = await client.post(
        f"{DOCKER}/containers/{UNKNOWN}/remove",
        json={"force": False},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 403, "auth.reauth_required")
    row = await db_scalar(
        app,
        select(AuditLog).where(
            AuditLog.action == "docker.container_action", AuditLog.result == "denied"
        ),
    )
    assert row is not None
    assert row.target == f"{UNKNOWN}:remove"
    assert row.error_code == "auth.reauth_required"


async def test_unknown_container_verb_is_validation_error(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(f"{DOCKER}/containers/{UNKNOWN}/explode", headers=csrf_headers(owner))
    assert_problem(resp, 422, "request.validation")


async def test_bad_container_id_in_path_is_validation_error(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.get(f"{DOCKER}/containers/bad id")
    assert_problem(resp, 422, "request.validation")


async def test_pull_request_body_is_validated(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(
        f"{DOCKER}/images/pull",
        json={"repository": "nginx; rm -rf /"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 422, "request.validation")


# ---------------------------------------------------------------------------
# API: degradation (helper down -> 502, daemon down -> 503)
# ---------------------------------------------------------------------------


async def test_helper_down_is_502(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    app.state.helper = HelperClient(Path("/nonexistent/mypanel-helper.sock"))
    resp = await client.get(f"{DOCKER}/containers")
    assert_problem(resp, 502, "helper.unavailable")


async def test_daemon_down_degrades(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, broken_docker: None
) -> None:
    # info stays 200 with available=false; everything else is 503.
    resp = await client.get(f"{DOCKER}/info")
    assert resp.status_code == 200
    assert resp.json() == {"available": False}
    resp = await client.get(f"{DOCKER}/containers")
    assert_problem(resp, 503, "docker.unavailable")
    resp = await client.get(f"{DOCKER}/stats")
    assert_problem(resp, 503, "docker.unavailable")


# ---------------------------------------------------------------------------
# API: end-to-end through a live helper socket + real daemon
# ---------------------------------------------------------------------------


@needs_daemon
async def test_reads_end_to_end(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient
) -> None:
    resp = await client.get(f"{DOCKER}/info")
    assert resp.status_code == 200
    assert resp.json()["available"] is True

    resp = await client.get(f"{DOCKER}/containers", params={"all": True})
    assert resp.status_code == 200
    for row in resp.json()["containers"]:
        assert set(row) == CONTAINER_ROW_FIELDS

    resp = await client.get(f"{DOCKER}/stats")
    assert resp.status_code == 200
    assert isinstance(resp.json()["stats"], dict)

    resp = await client.get(f"{DOCKER}/containers/{UNKNOWN}")
    assert_problem(resp, 404, "not_found")
    resp = await client.get(f"{DOCKER}/containers/{UNKNOWN}/logs")
    assert_problem(resp, 404, "not_found")


@needs_daemon
async def test_unknown_container_action_maps_to_404_and_audits(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient
) -> None:
    resp = await client.post(f"{DOCKER}/containers/{UNKNOWN}/start", headers=csrf_headers(owner))
    assert_problem(resp, 404, "not_found")
    row = await db_scalar(
        app,
        select(AuditLog).where(
            AuditLog.action == "docker.container_action", AuditLog.result == "denied"
        ),
    )
    assert row is not None
    assert row.error_code == "not_found"


@needs_daemon
async def test_unknown_image_remove_is_404(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient
) -> None:
    resp = await client.delete(f"{DOCKER}/images/{UNKNOWN}", headers=csrf_headers(owner))
    assert_problem(resp, 404, "not_found")


@needs_daemon
async def test_volume_lifecycle_end_to_end(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient
) -> None:
    name = f"mypanel-test-{uuid4().hex[:10]}"
    resp = await client.post(f"{DOCKER}/volumes", json={"name": name}, headers=csrf_headers(owner))
    assert resp.status_code == 200
    assert resp.json()["name"] == name
    try:
        resp = await client.get(f"{DOCKER}/volumes")
        assert any(v["name"] == name for v in resp.json()["volumes"])
    finally:
        resp = await client.delete(f"{DOCKER}/volumes/{name}", headers=csrf_headers(owner))
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    ok_row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "docker.volume_create", AuditLog.result == "ok"),
    )
    assert ok_row is not None
    assert ok_row.target == name
