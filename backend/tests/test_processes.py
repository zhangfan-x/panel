"""Processes domain tests (docs/code-plan-m2-m5.md §3.1 and §4.2).

Helper level: ``process.signal`` / ``process.renice`` allow and deny paths
against a real in-process HelperServer. API level: list/detail shape via
psutil, the CSRF and re-auth gates on the mutating endpoints, and an
end-to-end renice/signal flow through a live helper socket.
"""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import API, OwnerContext, SettingsFactory, csrf_headers, db_scalar
from fastapi import FastAPI
from sqlalchemy import select
from test_helper import running_helper

from ownpanel.config import Settings
from ownpanel.db.models import AuditLog
from ownpanel.errors import HelperActionError
from ownpanel.ipc.client import HelperClient

# Importing the actions package registers process.signal / process.renice.
importlib.import_module("ownpanel.helper.actions")

PROCESSES = f"{API}/processes"

LIST_FIELDS = {"pid", "ppid", "user", "cpu", "mem", "status", "started", "name", "cmdline"}


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


def spawn_dead_pid() -> int:
    """Pid of an already-reaped child: guaranteed stale until pid wrap-around."""
    proc = subprocess.Popen(["/bin/true"])
    proc.wait(timeout=5)
    return proc.pid


@pytest.fixture(scope="module")
def helper_socket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-wide helper socket path, shared by the server and the app settings."""
    return tmp_path_factory.mktemp("proc-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    """Client for a module-wide helper server that allows the current UID."""
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture
def settings(make_settings: SettingsFactory, helper_socket: Path) -> Settings:
    """App settings pointing at the module helper socket (live in e2e tests only)."""
    return make_settings(paths={"helper_socket": helper_socket})


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    """A disposable /bin/sleep child that we may signal and renice."""
    proc = subprocess.Popen(["/bin/sleep", "60"])
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# helper: process.signal
# ---------------------------------------------------------------------------


class TestHelperSignal:
    def test_sigterm_terminates_child(
        self, helper: HelperClient, sleeper: subprocess.Popen[bytes]
    ) -> None:
        result = helper.call("process.signal", {"pid": sleeper.pid, "signal": "SIGTERM"})
        assert result == {"ok": True}
        assert sleeper.wait(timeout=5) == -signal.SIGTERM

    def test_bad_signal_name_is_invalid_args(
        self, helper: HelperClient, sleeper: subprocess.Popen[bytes]
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("process.signal", {"pid": sleeper.pid, "signal": "SIGHUP"})
        assert excinfo.value.helper_code == "helper.invalid_args"
        assert sleeper.poll() is None  # the child was never touched

    def test_pid_1_is_denied(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("process.signal", {"pid": 1, "signal": "SIGKILL"})
        assert excinfo.value.helper_code == "helper.denied"

    def test_own_pid_is_denied(self, helper: HelperClient) -> None:
        # The in-process server shares our pid: this is the API self-protection.
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("process.signal", {"pid": os.getpid(), "signal": "SIGTERM"})
        assert excinfo.value.helper_code == "helper.denied"

    @pytest.mark.parametrize("pid", [0, -1], ids=["zero", "negative"])
    def test_group_pids_are_invalid_args(self, helper: HelperClient, pid: int) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("process.signal", {"pid": pid, "signal": "SIGTERM"})
        assert excinfo.value.helper_code == "helper.invalid_args"

    def test_stale_pid_is_not_found(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("process.signal", {"pid": spawn_dead_pid(), "signal": "SIGTERM"})
        assert excinfo.value.helper_code == "helper.not_found"


# ---------------------------------------------------------------------------
# helper: process.renice
# ---------------------------------------------------------------------------


class TestHelperRenice:
    def test_renice_child(self, helper: HelperClient, sleeper: subprocess.Popen[bytes]) -> None:
        result = helper.call("process.renice", {"pid": sleeper.pid, "nice": 5})
        assert result == {"nice": 5}
        assert os.getpriority(os.PRIO_PROCESS, sleeper.pid) == 5

    @pytest.mark.skipif(os.geteuid() == 0, reason="root may always lower nice values")
    def test_lowering_nice_without_privilege_is_denied(
        self, helper: HelperClient, sleeper: subprocess.Popen[bytes]
    ) -> None:
        before = os.getpriority(os.PRIO_PROCESS, sleeper.pid)
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("process.renice", {"pid": sleeper.pid, "nice": before - 5})
        assert excinfo.value.helper_code == "helper.denied"
        assert os.getpriority(os.PRIO_PROCESS, sleeper.pid) == before

    @pytest.mark.parametrize("nice", [-21, 20], ids=["below-min", "above-max"])
    def test_out_of_range_nice_is_invalid_args(
        self, helper: HelperClient, sleeper: subprocess.Popen[bytes], nice: int
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("process.renice", {"pid": sleeper.pid, "nice": nice})
        assert excinfo.value.helper_code == "helper.invalid_args"

    def test_stale_pid_is_not_found(self, helper: HelperClient) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("process.renice", {"pid": spawn_dead_pid(), "nice": 5})
        assert excinfo.value.helper_code == "helper.not_found"


# ---------------------------------------------------------------------------
# API: list and detail (API-side psutil, no helper involved)
# ---------------------------------------------------------------------------


async def test_list_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get(PROCESSES)
    assert_problem(resp, 401, "auth.required")


async def test_list_shape_and_search(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.get(PROCESSES, params={"search": "pytest", "limit": 1000})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["total"], int)
    assert body["total"] >= 1
    procs = body["processes"]
    assert len(procs) <= 1000
    mine = next(p for p in procs if p["pid"] == os.getpid())
    assert set(mine) == LIST_FIELDS  # exact field set is frontend contract
    assert mine["ppid"] == os.getppid()
    assert isinstance(mine["cpu"], int | float)
    assert isinstance(mine["mem"], int | float)
    assert "pytest" in (mine["name"] + mine["cmdline"]).lower()  # search matched us
    assert mine["started"].startswith("20")  # ISO timestamp


async def test_list_sort_and_pagination(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.get(PROCESSES, params={"sort": "pid", "order": "asc", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    pids = [p["pid"] for p in body["processes"]]
    assert 1 <= len(pids) <= 5
    assert pids == sorted(pids)
    assert body["total"] >= len(pids)

    resp = await client.get(
        PROCESSES, params={"sort": "pid", "order": "asc", "limit": 5, "offset": 2}
    )
    shifted = [p["pid"] for p in resp.json()["processes"]]
    assert shifted == sorted(shifted)
    assert shifted[0] > pids[0]  # the window moved past the lowest pids


async def test_list_rejects_bad_sort_key(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.get(PROCESSES, params={"sort": "evil"})
    assert_problem(resp, 422, "request.validation")


async def test_detail_reports_children(
    client: httpx.AsyncClient, owner: OwnerContext, sleeper: subprocess.Popen[bytes]
) -> None:
    resp = await client.get(f"{PROCESSES}/{os.getpid()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pid"] == os.getpid()
    assert set(body) >= LIST_FIELDS
    assert sleeper.pid in body["children"]
    assert body["nice"] == os.getpriority(os.PRIO_PROCESS, os.getpid())
    assert body["num_threads"] >= 1
    assert body["rss"] > 0


async def test_detail_unknown_pid_is_404(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.get(f"{PROCESSES}/{spawn_dead_pid()}")
    assert_problem(resp, 404, "not_found")


async def test_detail_rejects_pid_zero(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.get(f"{PROCESSES}/0")
    assert_problem(resp, 422, "request.validation")


# ---------------------------------------------------------------------------
# API: CSRF and re-auth gates on the mutating endpoints
# ---------------------------------------------------------------------------


async def test_renice_without_csrf_is_rejected(
    client: httpx.AsyncClient, owner: OwnerContext, sleeper: subprocess.Popen[bytes]
) -> None:
    before = os.getpriority(os.PRIO_PROCESS, sleeper.pid)
    resp = await client.post(f"{PROCESSES}/{sleeper.pid}/renice", json={"nice": 5})
    assert_problem(resp, 403, "csrf.invalid")
    assert os.getpriority(os.PRIO_PROCESS, sleeper.pid) == before


async def test_signal_without_csrf_is_rejected(
    client: httpx.AsyncClient, owner: OwnerContext, sleeper: subprocess.Popen[bytes]
) -> None:
    resp = await client.post(f"{PROCESSES}/{sleeper.pid}/signal", json={"signal": "SIGTERM"})
    assert_problem(resp, 403, "csrf.invalid")
    assert sleeper.poll() is None


async def test_signal_without_recent_reauth_is_rejected(
    client: httpx.AsyncClient, owner: OwnerContext, sleeper: subprocess.Popen[bytes]
) -> None:
    # Fresh sessions have reauth_at NULL; CSRF passes, so the re-auth gate trips.
    resp = await client.post(
        f"{PROCESSES}/{sleeper.pid}/signal",
        json={"signal": "SIGTERM"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 403, "auth.reauth_required")
    assert sleeper.poll() is None


# ---------------------------------------------------------------------------
# API end-to-end through a live helper socket
# ---------------------------------------------------------------------------


async def test_renice_and_signal_end_to_end(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    sleeper: subprocess.Popen[bytes],
) -> None:
    # Renice needs CSRF only.
    resp = await client.post(
        f"{PROCESSES}/{sleeper.pid}/renice", json={"nice": 5}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    assert resp.json() == {"nice": 5}
    assert os.getpriority(os.PRIO_PROCESS, sleeper.pid) == 5

    # Open the re-auth window, then SIGTERM the child.
    resp = await client.post(
        f"{API}/auth/reauth", json={"password": owner.password}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    resp = await client.post(
        f"{PROCESSES}/{sleeper.pid}/signal",
        json={"signal": "SIGTERM"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert sleeper.wait(timeout=5) == -signal.SIGTERM

    ok_row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "processes.signal", AuditLog.result == "ok"),
    )
    assert ok_row is not None
    assert ok_row.target == f"{sleeper.pid}:SIGTERM"

    # Helper policy denials surface as 400 helper.action_failed + helper_code.
    resp = await client.post(
        f"{PROCESSES}/1/signal", json={"signal": "SIGKILL"}, headers=csrf_headers(owner)
    )
    body = assert_problem(resp, 400, "helper.action_failed")
    assert body["helper_code"] == "helper.denied"
    denied_row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "processes.signal", AuditLog.result == "denied"),
    )
    assert denied_row is not None
    assert denied_row.error_code == "helper.action_failed"

    # The reaped child's pid is stale now: helper.not_found maps to HTTP 404.
    resp = await client.post(
        f"{PROCESSES}/{sleeper.pid}/signal",
        json={"signal": "SIGKILL"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 404, "not_found")
