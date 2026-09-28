"""Compose domain tests (docs/code-plan-m2-m5.md §3.5 and §4.6).

Helper level: discover/config/ps and the fd-streaming up/down/restart verbs
against a real in-process HelperServer with a fake ``docker`` CLI (argument
recording proves shell metacharacters in project paths never reach a shell).
API level: compose-root escape rejection, import/list/detail, YAML-validated
file editing via the ``files.*`` staging actions, CSRF/re-auth gates and an
end-to-end ``up`` job that streams subprocess output into the job log.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shlex
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import API, OwnerContext, SettingsFactory, csrf_headers, db_scalar
from fastapi import FastAPI
from sqlalchemy import select
from test_helper import running_helper

from mypanel.config import Settings
from mypanel.db.models import AuditLog, ComposeProject, Job
from mypanel.errors import HelperActionError
from mypanel.helper.actions import composesvc
from mypanel.helper.actions.composesvc import EXIT_MARKER, _parse_ps_output
from mypanel.ipc.client import HelperClient
from mypanel.ipc.messages import HELPER_NOT_FOUND, HELPER_PATH_INVALID

# Importing the actions package registers the compose.* (and files.*) handlers.
importlib.import_module("mypanel.helper.actions")

COMPOSE = f"{API}/compose"

VALID_COMPOSE = "services:\n  web:\n    image: nginx:alpine\n  db:\n    image: postgres:16\n"
UPDATED_COMPOSE = "services:\n  web:\n    image: nginx:1.27\n"
INVALID_YAML = "services: [oops\n"
NO_SERVICES_YAML = "volumes:\n  data: {}\n"

PS_JSON_ARRAY = '[{"Name":"alpha-web-1","State":"running","Status":"Up 5s","Ports":"80/tcp"}]'


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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def helper_socket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-wide helper socket path, shared by the server and the app settings."""
    return tmp_path_factory.mktemp("compose-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    """Client for a module-wide helper server that allows the current UID."""
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture
def compose_root(tmp_path: Path) -> Path:
    """Per-test compose root directory."""
    root = tmp_path / "compose-root"
    root.mkdir()
    return root


@pytest.fixture
def settings(make_settings: SettingsFactory, helper_socket: Path, compose_root: Path) -> Settings:
    """App settings pointing at the module helper socket and the tmp compose root.

    ``make_settings`` has no ``docker=`` knob (team-frozen conftest), so the
    docker section is merged through model_dump/model_validate.
    """
    base = make_settings(paths={"helper_socket": helper_socket})
    data = base.model_dump()
    data["docker"] = {"compose_root": str(compose_root)}
    return Settings.model_validate(data)


@dataclass
class FakeDocker:
    """Handle onto the fake docker CLI: recorded argv calls + forced exit code."""

    args_file: Path
    exit_file: Path

    def calls(self) -> list[list[str]]:
        """Recorded invocations, one argv list per call (without the argv[0])."""
        if not self.args_file.exists():
            return []
        calls: list[list[str]] = []
        current: list[str] = []
        for line in self.args_file.read_text(encoding="utf-8").splitlines():
            if line == "----":
                calls.append(current)
                current = []
            else:
                current.append(line)
        return calls


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    """Replace the fixed docker binary with a recording fake (no docker needed).

    The fake appends every argv (one element per line, ``----`` separator) to a
    file, answers ``ps`` with a JSON array and everything else with two output
    lines, and exits with the code stored in ``exit_file`` (default 0).
    """
    args_file = tmp_path / "docker-args.txt"
    exit_file = tmp_path / "docker-exit"
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        "{\n"
        "printf '%s\\n' \"$@\"\n"
        'echo "----"\n'
        f"}} >> {shlex.quote(str(args_file))}\n"
        'case "$*" in\n'
        '  *" ps "*)\n'
        f"    printf '%s\\n' {shlex.quote(PS_JSON_ARRAY)}\n"
        "    ;;\n"
        "  *)\n"
        '    echo "fake compose line 1"\n'
        '    echo "fake compose line 2"\n'
        "    ;;\n"
        "esac\n"
        f"if [ -f {shlex.quote(str(exit_file))} ]; then\n"
        f'  exit "$(cat {shlex.quote(str(exit_file))})"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(composesvc, "_DOCKER", str(script))
    return FakeDocker(args_file=args_file, exit_file=exit_file)


@pytest.fixture
def missing_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the compose actions at a docker binary that does not exist."""
    monkeypatch.setattr(composesvc, "_DOCKER", str(tmp_path / "missing-docker"))


def make_project(
    root: Path, name: str, content: str | None = VALID_COMPOSE, file_name: str = "compose.yaml"
) -> Path:
    """Create a project dir under the root, optionally with a compose file."""
    project = root / name
    project.mkdir()
    if content is not None:
        (project / file_name).write_text(content, encoding="utf-8")
    return project


def read_stream(fds: list[int]) -> str:
    """Drain a helper-returned stream descriptor to EOF (takes fd ownership)."""
    assert len(fds) == 1
    with os.fdopen(fds[0], "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


async def wait_for_job(
    client: httpx.AsyncClient, job_id: str, timeout_s: float = 8.0
) -> dict[str, Any]:
    """Poll the jobs API until the job reaches a terminal state."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = await client.get(f"{API}/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            return body
        await asyncio.sleep(0.05)
    pytest.fail(f"job {job_id} did not finish within {timeout_s}s")


# ---------------------------------------------------------------------------
# helper: compose.discover
# ---------------------------------------------------------------------------


class TestHelperDiscover:
    def test_lists_one_level_of_subdirs(self, helper: HelperClient, compose_root: Path) -> None:
        make_project(compose_root, "alpha")
        make_project(compose_root, "beta", content=None)
        (compose_root / ".hidden").mkdir()
        (compose_root / "not-a-dir").write_text("x", encoding="utf-8")

        result = helper.call("compose.discover", {"root": str(compose_root)})
        assert result == {
            "projects": [
                {
                    "name": "alpha",
                    "path": str(compose_root / "alpha"),
                    "has_compose_file": True,
                    "compose_file": "compose.yaml",
                },
                {
                    "name": "beta",
                    "path": str(compose_root / "beta"),
                    "has_compose_file": False,
                    "compose_file": None,
                },
            ]
        }

    def test_missing_root_is_empty(self, helper: HelperClient, tmp_path: Path) -> None:
        result = helper.call("compose.discover", {"root": str(tmp_path / "nope")})
        assert result == {"projects": []}

    @pytest.mark.parametrize(
        "root",
        ["relative/root", "/root/../etc", "//double", "/trailing/"],
        ids=["relative", "dotdot", "double-slash", "trailing-slash"],
    )
    def test_rejects_non_canonical_roots(self, helper: HelperClient, root: str) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("compose.discover", {"root": root})
        assert excinfo.value.helper_code == HELPER_PATH_INVALID


# ---------------------------------------------------------------------------
# helper: compose.config / compose.ps
# ---------------------------------------------------------------------------


class TestHelperConfig:
    def test_valid_file_checked_by_docker(
        self, helper: HelperClient, compose_root: Path, fake_docker: FakeDocker
    ) -> None:
        project = make_project(compose_root, "alpha")
        result = helper.call("compose.config", {"path": str(project)})
        assert result["valid"] is True
        assert result["services"] == ["db", "web"]  # sorted service names
        assert result["compose_file"] == "compose.yaml"
        assert result["checked_by"] == "docker"
        assert fake_docker.calls() == [
            ["compose", "--project-directory", str(project), "config", "--quiet"]
        ]

    def test_invalid_yaml_never_invokes_docker(
        self, helper: HelperClient, compose_root: Path, fake_docker: FakeDocker
    ) -> None:
        project = make_project(compose_root, "broken", content=INVALID_YAML)
        result = helper.call("compose.config", {"path": str(project)})
        assert result["valid"] is False
        assert result["services"] == []
        assert result["error"]
        assert fake_docker.calls() == []

    def test_missing_services_mapping_is_invalid(
        self, helper: HelperClient, compose_root: Path, fake_docker: FakeDocker
    ) -> None:
        project = make_project(compose_root, "noservices", content=NO_SERVICES_YAML)
        result = helper.call("compose.config", {"path": str(project)})
        assert result["valid"] is False
        assert "services" in str(result["error"])

    def test_no_compose_file_is_not_found(self, helper: HelperClient, compose_root: Path) -> None:
        project = make_project(compose_root, "empty", content=None)
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("compose.config", {"path": str(project)})
        assert excinfo.value.helper_code == HELPER_NOT_FOUND

    def test_yaml_verdict_stands_without_docker_cli(
        self, helper: HelperClient, compose_root: Path, missing_docker: None
    ) -> None:
        project = make_project(compose_root, "alpha")
        result = helper.call("compose.config", {"path": str(project)})
        assert result["valid"] is True
        assert result["checked_by"] == "yaml"
        assert result["services"] == ["db", "web"]

    def test_shell_metacharacters_stay_single_arguments(
        self, helper: HelperClient, compose_root: Path, fake_docker: FakeDocker, tmp_path: Path
    ) -> None:
        """Injection probe: the hostile path arrives as ONE argv element, unexpanded."""
        # A directory name may contain shell metacharacters but not "/" or NUL, so
        # the touch targets must be slash-free relative names; if the metacharacters
        # were ever expanded by a shell, these markers would appear somewhere.
        evil_name = "evil; touch pwned; $(touch pwned2) `touch pwned3`"
        project = make_project(compose_root, evil_name)
        result = helper.call("compose.config", {"path": str(project)})
        assert result["valid"] is True
        assert fake_docker.calls() == [
            ["compose", "--project-directory", str(project), "config", "--quiet"]
        ]
        for base in (compose_root, tmp_path, project, Path.cwd()):
            for name in ("pwned", "pwned2", "pwned3"):
                assert not (base / name).exists()

    def test_ps_parses_array_output(
        self, helper: HelperClient, compose_root: Path, fake_docker: FakeDocker
    ) -> None:
        project = make_project(compose_root, "alpha")
        result = helper.call("compose.ps", {"path": str(project)})
        assert result == {
            "services": [
                {"name": "alpha-web-1", "state": "running", "status": "Up 5s", "ports": "80/tcp"}
            ]
        }
        assert fake_docker.calls() == [
            ["compose", "--project-directory", str(project), "ps", "--all", "--format", "json"]
        ]

    def test_parse_ps_output_accepts_ndjson(self) -> None:
        ndjson = (
            '{"Name":"a-1","State":"running","Status":"Up","Ports":""}\n'
            '{"Name":"b-1","State":"exited","Status":"Exited (0)","Ports":""}\n'
        )
        assert [item["name"] for item in _parse_ps_output(ndjson)] == ["a-1", "b-1"]
        assert _parse_ps_output("") == []


# ---------------------------------------------------------------------------
# helper: compose.up / down / restart (fd streaming)
# ---------------------------------------------------------------------------


class TestHelperStreamVerbs:
    def test_up_streams_output_then_exit_marker(
        self, helper: HelperClient, compose_root: Path, fake_docker: FakeDocker
    ) -> None:
        project = make_project(compose_root, "alpha")
        result, fds = helper.call_with_fds("compose.up", {"path": str(project)})
        text = read_stream(fds)
        assert isinstance(result["pid"], int)
        assert "fake compose line 1" in text
        assert "fake compose line 2" in text
        assert text.rstrip("\n").endswith(f"{EXIT_MARKER}0")
        assert fake_docker.calls() == [
            ["compose", "--project-directory", str(project), "up", "-d", "--remove-orphans"]
        ]

    def test_down_failure_reports_exit_code_in_band(
        self, helper: HelperClient, compose_root: Path, fake_docker: FakeDocker
    ) -> None:
        fake_docker.exit_file.write_text("3", encoding="utf-8")
        project = make_project(compose_root, "alpha")
        _result, fds = helper.call_with_fds("compose.down", {"path": str(project)})
        assert read_stream(fds).rstrip("\n").endswith(f"{EXIT_MARKER}3")

    def test_up_without_compose_file_is_not_found(
        self, helper: HelperClient, compose_root: Path, fake_docker: FakeDocker
    ) -> None:
        project = make_project(compose_root, "empty", content=None)
        with pytest.raises(HelperActionError) as excinfo:
            helper.call_with_fds("compose.up", {"path": str(project)})
        assert excinfo.value.helper_code == HELPER_NOT_FOUND
        assert fake_docker.calls() == []


# ---------------------------------------------------------------------------
# API: auth / CSRF gates
# ---------------------------------------------------------------------------


async def test_list_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{COMPOSE}/projects")
    assert_problem(resp, 401, "auth.required")


async def test_mutations_without_csrf_are_rejected(
    client: httpx.AsyncClient, owner: OwnerContext, compose_root: Path
) -> None:
    project = make_project(compose_root, "alpha")
    resp = await client.post(f"{COMPOSE}/projects", json={"name": "alpha", "path": str(project)})
    assert_problem(resp, 403, "csrf.invalid")
    resp = await client.post(f"{COMPOSE}/projects/alpha/up")
    assert_problem(resp, 403, "csrf.invalid")
    resp = await client.put(f"{COMPOSE}/projects/alpha/file", json={"content": VALID_COMPOSE})
    assert_problem(resp, 403, "csrf.invalid")
    resp = await client.delete(f"{COMPOSE}/projects/alpha")
    assert_problem(resp, 403, "csrf.invalid")


# ---------------------------------------------------------------------------
# API: import / list / delete
# ---------------------------------------------------------------------------


async def test_import_and_list_merge(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    helper: HelperClient,
) -> None:
    alpha = make_project(compose_root, "alpha")
    make_project(compose_root, "beta", content=None)

    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "alpha", "path": str(alpha)},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"name": "alpha", "path": str(alpha), "imported": True}

    resp = await client.get(f"{COMPOSE}/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["compose_root"] == str(compose_root)
    by_name = {item["name"]: item for item in body["projects"]}
    assert by_name["alpha"]["imported"] is True
    assert by_name["alpha"]["has_compose_file"] is True
    assert by_name["alpha"]["compose_file"] == "compose.yaml"
    assert by_name["alpha"]["created_at"]
    assert by_name["beta"]["imported"] is False
    assert by_name["beta"]["has_compose_file"] is False

    row = await db_scalar(app, select(ComposeProject).where(ComposeProject.name == "alpha"))
    assert row is not None and row.path == str(alpha)
    audit = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "compose.import", AuditLog.result == "ok"),
    )
    assert audit is not None

    # Duplicate name or path is a conflict.
    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "alpha", "path": str(alpha)},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 409, "conflict")


async def test_import_rejects_paths_outside_compose_root(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    tmp_path: Path,
    helper: HelperClient,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "esc", "path": str(outside)},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 403, "forbidden")

    # A ".."-laden path that would RESOLVE inside the root is still malformed.
    sneaky = f"{compose_root}/../{compose_root.name}/alpha"
    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "esc2", "path": sneaky},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 422, "request.validation")

    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "esc3", "path": "relative/path"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 422, "request.validation")

    # The compose root itself is not a project.
    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "esc4", "path": str(compose_root)},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 403, "forbidden")

    # Hostile project names never reach the URL/table layer.
    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "../escape", "path": f"{compose_root}/x"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 422, "request.validation")


async def test_import_missing_directory_is_404(
    client: httpx.AsyncClient, owner: OwnerContext, compose_root: Path, helper: HelperClient
) -> None:
    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "ghost", "path": str(compose_root / "ghost")},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 404, "not_found")


async def test_delete_requires_recent_reauth(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    helper: HelperClient,
) -> None:
    alpha = make_project(compose_root, "alpha")
    resp = await client.post(
        f"{COMPOSE}/projects",
        json={"name": "alpha", "path": str(alpha)},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200

    # CSRF passes, but the fresh session has no recent re-auth.
    resp = await client.delete(f"{COMPOSE}/projects/alpha", headers=csrf_headers(owner))
    assert_problem(resp, 403, "auth.reauth_required")
    denied = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "compose.delete", AuditLog.result == "denied"),
    )
    assert denied is None  # the dependency rejected before the handler ran

    resp = await client.post(
        f"{API}/auth/reauth", json={"password": owner.password}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    resp = await client.delete(f"{COMPOSE}/projects/alpha", headers=csrf_headers(owner))
    assert resp.status_code == 204
    assert await db_scalar(app, select(ComposeProject)) is None

    # The directory is still on disk, so the project reverts to "discovered".
    resp = await client.get(f"{COMPOSE}/projects")
    by_name = {item["name"]: item for item in resp.json()["projects"]}
    assert by_name["alpha"]["imported"] is False

    ok_row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "compose.delete", AuditLog.result == "ok"),
    )
    assert ok_row is not None and ok_row.target == "alpha"


# ---------------------------------------------------------------------------
# API: detail, file read/write
# ---------------------------------------------------------------------------


async def test_detail_reports_config_and_ps(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    helper: HelperClient,
    fake_docker: FakeDocker,
) -> None:
    make_project(compose_root, "alpha")
    resp = await client.get(f"{COMPOSE}/projects/alpha")  # discovered, not imported
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "alpha"
    assert body["imported"] is False
    assert body["has_compose_file"] is True
    assert body["compose_file"] == "compose.yaml"
    assert body["config_valid"] is True
    assert body["config_checked_by"] == "docker"
    assert body["services"] == ["db", "web"]
    assert body["docker_available"] is True
    assert body["ps"] == [
        {"name": "alpha-web-1", "state": "running", "status": "Up 5s", "ports": "80/tcp"}
    ]


async def test_detail_degrades_without_docker_cli(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    helper: HelperClient,
    missing_docker: None,
) -> None:
    make_project(compose_root, "alpha")
    resp = await client.get(f"{COMPOSE}/projects/alpha")
    assert resp.status_code == 200
    body = resp.json()
    assert body["config_valid"] is True
    assert body["config_checked_by"] == "yaml"
    assert body["docker_available"] is False
    assert body["ps"] == []


async def test_detail_unknown_project_is_404(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient
) -> None:
    resp = await client.get(f"{COMPOSE}/projects/ghost")
    assert_problem(resp, 404, "not_found")
    resp = await client.get(f"{COMPOSE}/projects/bad!name")
    assert_problem(resp, 422, "request.validation")


async def test_file_read_write_roundtrip(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    helper: HelperClient,
) -> None:
    alpha = make_project(compose_root, "alpha")

    resp = await client.get(f"{COMPOSE}/projects/alpha/file")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["file"] == "compose.yaml"
    assert body["content"] == VALID_COMPOSE
    etag = body["etag"]
    assert etag

    resp = await client.put(
        f"{COMPOSE}/projects/alpha/file",
        json={"content": UPDATED_COMPOSE, "expected_etag": etag},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
    new_etag = resp.json()["etag"]
    assert new_etag and new_etag != etag
    assert (alpha / "compose.yaml").read_text(encoding="utf-8") == UPDATED_COMPOSE
    # No staging leftovers in the project directory.
    assert [p.name for p in alpha.iterdir()] == ["compose.yaml"]

    # Stale etag: the file changed since it was read.
    resp = await client.put(
        f"{COMPOSE}/projects/alpha/file",
        json={"content": VALID_COMPOSE, "expected_etag": etag},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 409, "conflict")
    assert (alpha / "compose.yaml").read_text(encoding="utf-8") == UPDATED_COMPOSE


async def test_file_write_rejects_invalid_yaml(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    helper: HelperClient,
) -> None:
    alpha = make_project(compose_root, "alpha")
    for content in (INVALID_YAML, NO_SERVICES_YAML):
        resp = await client.put(
            f"{COMPOSE}/projects/alpha/file",
            json={"content": content},
            headers=csrf_headers(owner),
        )
        assert_problem(resp, 422, "request.validation")
    assert (alpha / "compose.yaml").read_text(encoding="utf-8") == VALID_COMPOSE


async def test_file_missing_is_404(
    client: httpx.AsyncClient, owner: OwnerContext, compose_root: Path, helper: HelperClient
) -> None:
    make_project(compose_root, "empty", content=None)
    resp = await client.get(f"{COMPOSE}/projects/empty/file")
    assert_problem(resp, 404, "not_found")


# ---------------------------------------------------------------------------
# API: up/down/restart jobs end-to-end
# ---------------------------------------------------------------------------


async def test_up_job_streams_output_end_to_end(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    settings: Settings,
    helper: HelperClient,
    fake_docker: FakeDocker,
) -> None:
    project = make_project(compose_root, "alpha")
    resp = await client.post(f"{COMPOSE}/projects/alpha/up", headers=csrf_headers(owner))
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    job = await wait_for_job(client, job_id)
    assert job["status"] == "succeeded", job

    row = await db_scalar(app, select(Job).where(Job.id == job_id))
    assert row is not None
    assert row.type == "compose.up"
    assert row.resource_key == f"compose:{project}"

    log_text = (settings.paths.data_dir / "jobs" / f"{job_id}.log").read_text(encoding="utf-8")
    assert "fake compose line 1" in log_text
    assert EXIT_MARKER not in log_text  # the marker is consumed, not logged

    audit = await db_scalar(
        app, select(AuditLog).where(AuditLog.action == "compose.up", AuditLog.result == "ok")
    )
    assert audit is not None and audit.target == "alpha"


async def test_failing_verb_fails_the_job(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    helper: HelperClient,
    fake_docker: FakeDocker,
) -> None:
    make_project(compose_root, "alpha")
    fake_docker.exit_file.write_text("7", encoding="utf-8")
    resp = await client.post(f"{COMPOSE}/projects/alpha/down", headers=csrf_headers(owner))
    assert resp.status_code == 202
    job = await wait_for_job(client, resp.json()["job_id"])
    assert job["status"] == "failed"
    assert "7" in (job["error_message"] or "")


async def test_up_without_compose_file_fails_the_job(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    compose_root: Path,
    helper: HelperClient,
    fake_docker: FakeDocker,
) -> None:
    make_project(compose_root, "empty", content=None)
    resp = await client.post(f"{COMPOSE}/projects/empty/up", headers=csrf_headers(owner))
    assert resp.status_code == 202
    job = await wait_for_job(client, resp.json()["job_id"])
    assert job["status"] == "failed"


async def test_unknown_verb_is_rejected(
    client: httpx.AsyncClient, owner: OwnerContext, compose_root: Path, helper: HelperClient
) -> None:
    make_project(compose_root, "alpha")
    resp = await client.post(f"{COMPOSE}/projects/alpha/pause", headers=csrf_headers(owner))
    assert_problem(resp, 422, "request.validation")
