"""Services domain tests (docs/code-plan-m2-m5.md §3.2 and §4.3).

Helper level: strict unit-name validation (injection attempts must never reach
a subprocess), existence cross-check before lifecycle verbs, list/status/journal
parsing against canned systemctl/journalctl output, and the journal follow fd
hand-off. API level: auth/CSRF gates, helper-code-to-status mapping, audit rows
and the journal SSE stream — all through a live in-process helper socket.
subprocess output is canned via monkeypatch because systemctl needs root (or a
systemd host) for most operations while tests must run unprivileged.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
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
from ownpanel.helper.actions import systemd as systemd_actions
from ownpanel.ipc.client import HelperClient
from ownpanel.ipc.fdpass import close_fds

# Importing the actions package registers every systemd.* action.
importlib.import_module("ownpanel.helper.actions")

SERVICES = f"{API}/services"

SYSTEMCTL = "/usr/bin/systemctl"

LIST_UNITS_JSON = json.dumps(
    [
        {
            "unit": "nginx.service",
            "load": "loaded",
            "active": "active",
            "sub": "running",
            "description": "web server",
        },
        {
            "unit": "cron.service",
            "load": "loaded",
            "active": "inactive",
            "sub": "dead",
            "description": "cron daemon",
        },
    ]
)

LIST_UNIT_FILES_JSON = json.dumps(
    [
        {"unit_file": "nginx.service", "state": "enabled", "vendor_preset": "enabled"},
        {"unit_file": "ghost.service", "state": "disabled", "vendor_preset": None},
    ]
)

STATUS_SHOW = (
    "Id=nginx.service\n"
    "LoadState=loaded\n"
    "ActiveState=active\n"
    "SubState=running\n"
    "UnitFileState=enabled\n"
    "Description=A = B server\n"
    "MainPID=1234\n"
    "MemoryCurrent=2048\n"
    "ActiveEnterTimestamp=Tue 2026-07-21 10:00:00 CST\n"
    "FragmentPath=/lib/systemd/system/nginx.service\n"
)

JOURNAL_LINE_FULL = (
    '{"__REALTIME_TIMESTAMP":"1700000000000000","MESSAGE":"hello",'
    '"PRIORITY":"6","_SYSTEMD_UNIT":"nginx.service"}'
)
JOURNAL_LINE_BINARY = '{"MESSAGE":[104,105],"PRIORITY":"3"}'
JOURNAL_STDOUT = f"{JOURNAL_LINE_FULL}\n{JOURNAL_LINE_BINARY}\nnot-json\n"

JOURNAL_TS = "2023-11-14T22:13:20+00:00"

INJECTION_UNITS = [
    pytest.param("a;b.service", id="semicolon"),
    pytest.param("../x.service", id="path-traversal"),
    pytest.param("a b.service", id="whitespace"),
    pytest.param("`id`.service", id="backtick"),
    pytest.param("$(reboot).service", id="subshell"),
    pytest.param("a|b.service", id="pipe"),
    pytest.param("a\nb.service", id="newline"),
    pytest.param("a..b.service", id="dotdot"),
    pytest.param("/etc/x.service", id="absolute-path"),
    pytest.param("nginx.conf", id="bad-suffix"),
]


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


class FakeRun:
    """Canned ``_run`` replacement: records argv, answers by subcommand.

    Keys are the systemctl subcommand (``list-units``, ``show``, ``start``...)
    or ``"journal"`` for journalctl invocations; unknown keys succeed silently.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.responses: dict[str, tuple[int, str, str]] = {}

    def __call__(self, argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        key = "journal" if argv[0].endswith("journalctl") else argv[1]
        code, out, err = self.responses.get(key, (0, "", ""))
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(systemd_actions, "_run", fake)
    return fake


@pytest.fixture
def fake_journalctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in journalctl that prints two JSON entries and exits (EOF)."""
    script = tmp_path / "fake-journalctl"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '{JOURNAL_LINE_FULL.replace('nginx', 'fake')}'\n"
        "printf '%s\\n' '{\"MESSAGE\":\"world\"}'\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(systemd_actions, "_JOURNALCTL", str(script))
    return script


@pytest.fixture(scope="module")
def helper_socket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-wide helper socket path, shared by the server and the app settings."""
    return tmp_path_factory.mktemp("svc-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    """Client for a module-wide helper server that allows the current UID."""
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture
def settings(make_settings: SettingsFactory, helper_socket: Path) -> Settings:
    """App settings pointing at the module helper socket."""
    return make_settings(paths={"helper_socket": helper_socket})


# ---------------------------------------------------------------------------
# helper: unit-name validation (injection can never reach a subprocess)
# ---------------------------------------------------------------------------


class TestUnitValidation:
    @pytest.mark.parametrize("unit", INJECTION_UNITS)
    def test_mutating_verb_rejects_bad_names(
        self, helper: HelperClient, fake_run: FakeRun, unit: str
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("systemd.start", {"unit": unit})
        assert excinfo.value.helper_code == "helper.invalid_args"
        assert fake_run.calls == []  # rejected before any subprocess

    @pytest.mark.parametrize("action", ["systemd.status", "systemd.journal"])
    def test_read_actions_reject_bad_names(
        self, helper: HelperClient, fake_run: FakeRun, action: str
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call(action, {"unit": "a;b.service"})
        assert excinfo.value.helper_code == "helper.invalid_args"
        assert fake_run.calls == []

    def test_journal_follow_rejects_bad_names(
        self, helper: HelperClient, fake_run: FakeRun
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call_with_fds("systemd.journal", {"unit": "`id`.service", "follow": True})
        assert excinfo.value.helper_code == "helper.invalid_args"
        assert fake_run.calls == []

    def test_journal_lines_out_of_range_is_invalid_args(
        self, helper: HelperClient, fake_run: FakeRun
    ) -> None:
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("systemd.journal", {"unit": "nginx.service", "lines": 100_000})
        assert excinfo.value.helper_code == "helper.invalid_args"
        assert fake_run.calls == []


# ---------------------------------------------------------------------------
# helper: lifecycle verbs (existence cross-check, argv shape, failure mapping)
# ---------------------------------------------------------------------------


class TestHelperVerbs:
    def test_verb_requires_existing_unit(self, helper: HelperClient, fake_run: FakeRun) -> None:
        fake_run.responses["show"] = (0, "LoadState=not-found\n", "")
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("systemd.start", {"unit": "ghost.service"})
        assert excinfo.value.helper_code == "helper.not_found"
        assert [c[1] for c in fake_run.calls] == ["show"]  # the verb never ran

    def test_start_runs_systemctl_with_separator(
        self, helper: HelperClient, fake_run: FakeRun
    ) -> None:
        fake_run.responses["show"] = (0, "LoadState=loaded\n", "")
        result = helper.call("systemd.start", {"unit": "nginx.service"})
        assert result == {"ok": True}
        # Absolute path, arg array, and `--` so a unit can't be parsed as a flag.
        assert fake_run.calls[-1] == [SYSTEMCTL, "start", "--", "nginx.service"]

    def test_masked_unit_failure_maps_to_denied(
        self, helper: HelperClient, fake_run: FakeRun
    ) -> None:
        fake_run.responses["show"] = (0, "LoadState=masked\n", "")
        fake_run.responses["start"] = (1, "", "Unit nginx.service is masked.")
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("systemd.start", {"unit": "nginx.service"})
        assert excinfo.value.helper_code == "helper.denied"

    def test_permission_failure_maps_to_denied(
        self, helper: HelperClient, fake_run: FakeRun
    ) -> None:
        fake_run.responses["show"] = (0, "LoadState=loaded\n", "")
        fake_run.responses["stop"] = (4, "", "Access denied")
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("systemd.stop", {"unit": "nginx.service"})
        assert excinfo.value.helper_code == "helper.denied"


# ---------------------------------------------------------------------------
# helper: list_units / status / journal parsing
# ---------------------------------------------------------------------------


class TestHelperReads:
    def test_list_units_merges_enabled_state(self, helper: HelperClient, fake_run: FakeRun) -> None:
        fake_run.responses["list-units"] = (0, LIST_UNITS_JSON, "")
        fake_run.responses["list-unit-files"] = (0, LIST_UNIT_FILES_JSON, "")
        result = helper.call("systemd.list_units")
        units = result["units"]
        assert [u["name"] for u in units] == ["cron.service", "ghost.service", "nginx.service"]
        nginx = units[2]
        assert nginx == {
            "name": "nginx.service",
            "load": "loaded",
            "active": "active",
            "sub": "running",
            "description": "web server",
            "enabled": "enabled",
        }
        assert units[0]["enabled"] == ""  # loaded but has no unit file entry
        ghost = units[1]  # installed but not loaded: synthesized placeholder
        assert ghost["active"] == "inactive"
        assert ghost["sub"] == "dead"
        assert ghost["enabled"] == "disabled"

    def test_status_parses_show_output(self, helper: HelperClient, fake_run: FakeRun) -> None:
        fake_run.responses["show"] = (0, STATUS_SHOW, "")
        result = helper.call("systemd.status", {"unit": "nginx.service"})
        assert result == {
            "name": "nginx.service",
            "active": "active",
            "sub": "running",
            "load": "loaded",
            "enabled": "enabled",
            "description": "A = B server",  # '=' inside a value survives
            "main_pid": 1234,
            "memory": 2048,
            "since": "Tue 2026-07-21 10:00:00 CST",
            "fragment_path": "/lib/systemd/system/nginx.service",
        }
        argv = fake_run.calls[-1]
        assert argv[0] == SYSTEMCTL
        assert argv[-2:] == ["--", "nginx.service"]

        fake_run.responses["show"] = (0, STATUS_SHOW.replace("=2048", "=[not set]"), "")
        result = helper.call("systemd.status", {"unit": "nginx.service"})
        assert result["memory"] is None

    def test_status_unknown_unit_is_not_found(
        self, helper: HelperClient, fake_run: FakeRun
    ) -> None:
        fake_run.responses["show"] = (0, "LoadState=not-found\n", "")
        with pytest.raises(HelperActionError) as excinfo:
            helper.call("systemd.status", {"unit": "ghost.service"})
        assert excinfo.value.helper_code == "helper.not_found"

    def test_journal_snapshot_parses_lines(self, helper: HelperClient, fake_run: FakeRun) -> None:
        fake_run.responses["journal"] = (0, JOURNAL_STDOUT, "")
        result = helper.call("systemd.journal", {"unit": "nginx.service", "lines": 50})
        assert result["lines"] == [
            {"ts": JOURNAL_TS, "message": "hello", "priority": 6, "unit": "nginx.service"},
            # Binary MESSAGE (byte array) decoded, missing fields defaulted.
            {"ts": "", "message": "hi", "priority": 3, "unit": "nginx.service"},
        ]
        argv = fake_run.calls[-1]
        assert argv[1:3] == ["-u", "nginx.service"]
        assert "-n" in argv
        assert argv[argv.index("-n") + 1] == "50"
        assert "-f" not in argv

    def test_journal_follow_passes_pipe_fd(
        self, helper: HelperClient, fake_journalctl: Path
    ) -> None:
        result, fds = helper.call_with_fds(
            "systemd.journal", {"unit": "fake.service", "follow": True, "lines": 10}
        )
        try:
            assert len(fds) == 1
            pid = result["pid"]
            assert pid in systemd_actions._FOLLOWERS  # kept alive against GC
            data = b""
            while chunk := os.read(fds[0], 65536):
                data += chunk  # script exits after two lines -> EOF, no hang
        finally:
            close_fds(fds)
        messages = [json.loads(line)["MESSAGE"] for line in data.decode().splitlines()]
        assert messages == ["hello", "world"]
        assert systemd_actions._FOLLOWERS[pid].wait(timeout=5) == 0
        systemd_actions._reap_followers()
        assert pid not in systemd_actions._FOLLOWERS


@pytest.mark.skipif(not os.path.exists(SYSTEMCTL), reason="systemctl not installed")
def test_list_units_against_real_systemd(helper: HelperClient) -> None:
    """On a live systemd host the un-mocked list works without privilege."""
    try:
        result = helper.call("systemd.list_units", timeout_s=30.0)
    except HelperActionError as exc:  # e.g. container without a systemd manager
        pytest.skip(f"systemd unavailable: {exc.helper_code}")
    units = result["units"]
    if not units:
        pytest.skip("no service units visible")
    row = units[0]
    assert set(row) == {"name", "load", "active", "sub", "description", "enabled"}
    assert row["name"].endswith(".service")


# ---------------------------------------------------------------------------
# API: auth / CSRF / validation gates
# ---------------------------------------------------------------------------


async def test_api_list_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get(SERVICES)
    assert_problem(resp, 401, "auth.required")


async def test_api_stream_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{SERVICES}/nginx.service/journal/stream")
    assert_problem(resp, 401, "auth.required")


async def test_api_action_without_csrf_is_rejected(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(f"{SERVICES}/nginx.service/restart")
    assert_problem(resp, 403, "csrf.invalid")


async def test_api_action_unknown_verb_is_422(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(f"{SERVICES}/nginx.service/frobnicate", headers=csrf_headers(owner))
    assert_problem(resp, 422, "request.validation")


async def test_api_journal_lines_out_of_range_is_422(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.get(f"{SERVICES}/nginx.service/journal", params={"lines": 0})
    assert_problem(resp, 422, "request.validation")


# ---------------------------------------------------------------------------
# API: end-to-end through the live helper socket (canned subprocess output)
# ---------------------------------------------------------------------------


async def test_api_action_invalid_unit_maps_to_helper_error(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_run: FakeRun,
) -> None:
    resp = await client.post(f"{SERVICES}/a;b.service/start", headers=csrf_headers(owner))
    body = assert_problem(resp, 400, "helper.action_failed")
    assert body["helper_code"] == "helper.invalid_args"
    assert fake_run.calls == []
    denied = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "services.start", AuditLog.result == "denied"),
    )
    assert denied is not None
    assert denied.target == "a;b.service"
    assert denied.error_code == "helper.action_failed"


async def test_api_action_success_records_audit(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_run: FakeRun,
) -> None:
    fake_run.responses["show"] = (0, "LoadState=loaded\n", "")
    resp = await client.post(f"{SERVICES}/nginx.service/restart", headers=csrf_headers(owner))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert fake_run.calls[-1] == [SYSTEMCTL, "restart", "--", "nginx.service"]
    ok_row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "services.restart", AuditLog.result == "ok"),
    )
    assert ok_row is not None
    assert ok_row.target == "nginx.service"


async def test_api_detail_unknown_unit_is_404(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, fake_run: FakeRun
) -> None:
    fake_run.responses["show"] = (0, "LoadState=not-found\n", "")
    resp = await client.get(f"{SERVICES}/ghost.service")
    assert_problem(resp, 404, "not_found")


async def test_api_list_shape(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, fake_run: FakeRun
) -> None:
    fake_run.responses["list-units"] = (0, LIST_UNITS_JSON, "")
    fake_run.responses["list-unit-files"] = (0, LIST_UNIT_FILES_JSON, "")
    resp = await client.get(SERVICES)
    assert resp.status_code == 200
    units = resp.json()["units"]
    assert len(units) == 3
    assert {"name", "load", "active", "sub", "description", "enabled"} == set(units[0])


async def test_api_journal_snapshot(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, fake_run: FakeRun
) -> None:
    fake_run.responses["journal"] = (0, JOURNAL_STDOUT, "")
    resp = await client.get(f"{SERVICES}/nginx.service/journal", params={"lines": 50})
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert lines[0] == {
        "ts": JOURNAL_TS,
        "message": "hello",
        "priority": 6,
        "unit": "nginx.service",
    }
    assert len(lines) == 2


async def test_api_journal_stream_sse(
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_journalctl: Path,
) -> None:
    # The fake journalctl exits after two entries, so EOF ends the stream and
    # the whole response can be awaited; the timeout guards against regressions.
    resp = await asyncio.wait_for(client.get(f"{SERVICES}/fake.service/journal/stream"), timeout=15)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text.startswith("event: ready\n")
    frames = [f for f in resp.text.split("\n\n") if f.startswith("event: journal")]
    assert len(frames) == 2
    first = json.loads(frames[0].split("data: ", 1)[1])
    assert first == {"ts": JOURNAL_TS, "message": "hello", "priority": 6, "unit": "fake.service"}
    second = json.loads(frames[1].split("data: ", 1)[1])
    assert second["message"] == "world"
    assert second["unit"] == "fake.service"  # fallback fills the missing unit
    assert second["priority"] == 6  # default when PRIORITY is absent
