"""Docker daemon configuration tests (``/etc/docker/daemon.json``).

Helper level: the ``dockercfg.*`` actions are exercised against a temporary
directory — :data:`mypanel.helper.actions.dockercfg.DAEMON_JSON` is
monkeypatched, and because the helper server runs in-process (a thread, see
``test_helper.running_helper``) that patch is visible to its handlers too, so
nothing here can touch the real system config.

API level: auth/CSRF/re-auth gates, round-tripping through the helper, and
audit rows. ``POST /docker/daemon-restart`` is deliberately only tested on its
*rejection* paths — the success path would really restart the host's Docker
daemon.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from conftest import (
    API,
    OwnerContext,
    SettingsFactory,
    csrf_headers,
    db_execute,
    db_scalar,
    naive_utcnow,
)
from fastapi import FastAPI
from sqlalchemy import select, update
from test_helper import running_helper

from mypanel.config import Settings
from mypanel.db.models import AuditLog, Session
from mypanel.helper.actions import dockercfg
from mypanel.helper.registry import ActionError
from mypanel.ipc.client import HelperClient

importlib.import_module("mypanel.helper.actions")

DOCKER = f"{API}/docker"
CONFIG = f"{DOCKER}/daemon-config"


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the daemon.json constants at a temp dir for the whole process."""
    target = tmp_path / "docker"
    target.mkdir()
    monkeypatch.setattr(dockercfg, "DAEMON_DIR", str(target))
    monkeypatch.setattr(dockercfg, "DAEMON_JSON", str(target / "daemon.json"))
    return target


@pytest.fixture(scope="module")
def helper_socket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("dockercfg-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture
def settings(make_settings: SettingsFactory, helper_socket: Path) -> Settings:
    return make_settings(paths={"helper_socket": helper_socket})


async def reauth(app: FastAPI) -> None:
    """Mark the current session as recently re-authenticated."""
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow()))


# ---------------------------------------------------------------------------
# helper actions
# ---------------------------------------------------------------------------


def test_read_reports_missing_file_without_error(cfg_dir: Path) -> None:
    """Docker runs fine with no daemon.json, so absence is a state, not a failure."""
    result = dockercfg.dockercfg_read(dockercfg.EmptyArgs())
    assert result["exists"] is False
    assert result["content"] == ""
    assert result["mtime"] == 0.0


def test_write_normalises_and_reads_back(cfg_dir: Path) -> None:
    dockercfg.dockercfg_write(dockercfg.WriteArgs(content='{"live-restore":true}'))
    written = (cfg_dir / "daemon.json").read_text(encoding="utf-8")
    # Stored canonically regardless of the whitespace the client sent.
    assert written == '{\n  "live-restore": true\n}\n'
    assert (cfg_dir / "daemon.json").stat().st_mode & 0o777 == 0o644

    result = dockercfg.dockercfg_read(dockercfg.EmptyArgs())
    assert result["exists"] is True
    assert json.loads(result["content"]) == {"live-restore": True}
    assert result["mtime"] > 0


def test_write_rejects_invalid_json(cfg_dir: Path) -> None:
    """A daemon that cannot parse its config refuses to start; catch it here."""
    with pytest.raises(ActionError) as excinfo:
        dockercfg.dockercfg_write(dockercfg.WriteArgs(content='{"a": '))
    assert excinfo.value.code == "helper.invalid_args"
    assert not (cfg_dir / "daemon.json").exists()


def test_write_rejects_non_object_top_level(cfg_dir: Path) -> None:
    with pytest.raises(ActionError) as excinfo:
        dockercfg.dockercfg_write(dockercfg.WriteArgs(content="[1, 2]"))
    assert excinfo.value.code == "helper.invalid_args"


def test_write_treats_empty_content_as_empty_object(cfg_dir: Path) -> None:
    dockercfg.dockercfg_write(dockercfg.WriteArgs(content="   "))
    assert json.loads((cfg_dir / "daemon.json").read_text(encoding="utf-8")) == {}


def test_write_backs_up_previous_content(cfg_dir: Path) -> None:
    dockercfg.dockercfg_write(dockercfg.WriteArgs(content='{"log-driver": "local"}'))
    result = dockercfg.dockercfg_write(dockercfg.WriteArgs(content='{"log-driver": "json-file"}'))

    backup = result["backup"]
    assert backup.startswith("daemon.json.bak-")
    assert json.loads((cfg_dir / backup).read_text(encoding="utf-8")) == {"log-driver": "local"}
    assert json.loads((cfg_dir / "daemon.json").read_text(encoding="utf-8")) == {
        "log-driver": "json-file"
    }


def test_first_write_has_no_backup(cfg_dir: Path) -> None:
    result = dockercfg.dockercfg_write(dockercfg.WriteArgs(content="{}"))
    assert result["backup"] == ""


def test_backups_are_pruned_to_the_newest_few(cfg_dir: Path) -> None:
    (cfg_dir / "daemon.json").write_text("{}", encoding="utf-8")
    for i in range(8):
        # Pre-seed distinct timestamps: same-second writes would collide.
        (cfg_dir / f"daemon.json.bak-2026080100000{i}").write_text("{}", encoding="utf-8")
    dockercfg.dockercfg_write(dockercfg.WriteArgs(content='{"x": 1}'))

    backups = sorted(n for n in os.listdir(cfg_dir) if n.startswith("daemon.json.bak-"))
    assert len(backups) == dockercfg._KEEP_BACKUPS
    # The oldest ones went, the newest survived.
    assert "daemon.json.bak-20260801000000" not in backups
    assert "daemon.json.bak-20260801000007" in backups


# ---------------------------------------------------------------------------
# API: read
# ---------------------------------------------------------------------------


async def test_get_config_requires_authentication(client: httpx.AsyncClient) -> None:
    resp = await client.get(CONFIG)
    assert resp.status_code == 401


async def test_get_config_returns_parsed_object(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, cfg_dir: Path
) -> None:
    (cfg_dir / "daemon.json").write_text(
        json.dumps({"registry-mirrors": ["https://m.example.com"]}), encoding="utf-8"
    )
    resp = await client.get(CONFIG)
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is True
    assert body["parsed"] == {"registry-mirrors": ["https://m.example.com"]}
    assert body["parse_error"] == ""


async def test_get_config_surfaces_parse_errors_but_keeps_raw_text(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, cfg_dir: Path
) -> None:
    """A hand-broken config must still be readable so the editor can fix it."""
    (cfg_dir / "daemon.json").write_text('{"oops": ', encoding="utf-8")
    resp = await client.get(CONFIG)
    assert resp.status_code == 200
    body = resp.json()
    assert body["parsed"] == {}
    assert body["parse_error"]
    assert body["content"] == '{"oops": '


async def test_get_config_reports_missing_file(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, cfg_dir: Path
) -> None:
    resp = await client.get(CONFIG)
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is False
    assert body["restart_pending"] is False


# ---------------------------------------------------------------------------
# API: write
# ---------------------------------------------------------------------------


async def test_put_config_requires_csrf(
    client: httpx.AsyncClient, owner: OwnerContext, cfg_dir: Path
) -> None:
    resp = await client.put(CONFIG, json={"content": "{}"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf.invalid"


async def test_put_config_requires_recent_reauth(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, cfg_dir: Path
) -> None:
    resp = await client.put(CONFIG, json={"content": "{}"}, headers=csrf_headers(owner))
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.reauth_required"
    assert not (cfg_dir / "daemon.json").exists()


async def test_put_config_writes_and_audits(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    cfg_dir: Path,
) -> None:
    await reauth(app)
    resp = await client.put(
        CONFIG,
        json={"content": '{"proxies": {"http-proxy": "http://127.0.0.1:7890"}}'},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert json.loads((cfg_dir / "daemon.json").read_text(encoding="utf-8")) == {
        "proxies": {"http-proxy": "http://127.0.0.1:7890"}
    }

    row = await db_scalar(
        app, select(AuditLog).where(AuditLog.action == "docker.daemon_config_write")
    )
    assert row is not None
    assert row.result == "ok"


async def test_put_config_rejects_invalid_json_and_audits_denial(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    cfg_dir: Path,
) -> None:
    await reauth(app)
    resp = await client.put(CONFIG, json={"content": "not json"}, headers=csrf_headers(owner))
    assert resp.status_code == 400
    assert not (cfg_dir / "daemon.json").exists()

    row = await db_scalar(
        app,
        select(AuditLog).where(
            AuditLog.action == "docker.daemon_config_write", AuditLog.result == "denied"
        ),
    )
    assert row is not None


# ---------------------------------------------------------------------------
# API: restart (rejection paths only — a success would restart the host daemon)
# ---------------------------------------------------------------------------


async def test_restart_requires_csrf(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.post(f"{DOCKER}/daemon-restart")
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf.invalid"


async def test_restart_requires_recent_reauth(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(f"{DOCKER}/daemon-restart", headers=csrf_headers(owner))
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.reauth_required"
