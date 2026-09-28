"""Website (nginx) domain tests (FEATURE: website module).

Helper level: site name validation (``..`` / ``/`` / NUL rejected), the
write→read→list→enable/disable/delete site lifecycle against a real in-process
HelperServer with a temporary ``nginx_dir``, and the certificate actions
(self-signed generation + parsing, upload, delete) using the real ``openssl``
binary. API level: the auth/CSRF gates, the audited site/cert mutations, and the
test-then-reload flow driven by fake ``nginx`` / ``systemctl`` binaries so no
root privilege or real service is touched.
"""

from __future__ import annotations

import importlib
import os
import shlex
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import API, OwnerContext, SettingsFactory, csrf_headers, db_scalar
from fastapi import FastAPI
from sqlalchemy import func, select
from test_helper import running_helper

from ownpanel.config import Settings
from ownpanel.db.models import AuditLog
from ownpanel.errors import HelperActionError
from ownpanel.helper.actions import nginx
from ownpanel.ipc.client import HelperClient
from ownpanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_INVALID_ARGS,
    HELPER_NOT_FOUND,
    HELPER_PATH_INVALID,
)

# Importing the actions package registers the nginx.* (and files.*) handlers.
importlib.import_module("ownpanel.helper.actions")

WEBSITE = f"{API}/website"
OPENSSL = "/usr/bin/openssl"

SAMPLE_SITE = (
    "server {\n"
    "    listen 80;\n"
    "    server_name example.com www.example.com;\n"
    "    root /var/www/example;\n"
    "}\n"
)
BETA_SITE = (
    "server {\n  listen 8443 ssl;\n  location / {\n    proxy_pass http://127.0.0.1:3000;\n  }\n}\n"
)


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
    return tmp_path_factory.mktemp("website-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    """Client for a module-wide helper server that allows the current UID."""
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture(scope="module")
def pem_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """A real (cert_pem, key_pem) pair generated once with openssl for upload tests."""
    d = tmp_path_factory.mktemp("pem")
    subprocess.run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(d / "k"),
            "-out",
            str(d / "c"),
            "-days",
            "10",
            "-subj",
            "/CN=upload.test",
        ],
        check=True,
        capture_output=True,
    )
    return (d / "c").read_text(), (d / "k").read_text()


@pytest.fixture
def nginx_dir(tmp_path: Path) -> Path:
    """Per-test nginx base dir; sites-available/enabled are created by the helper."""
    d = tmp_path / "nginx"
    d.mkdir()
    return d


@pytest.fixture
def cert_dir(tmp_path: Path) -> Path:
    """Per-test cert dir path — deliberately NOT created (the helper makes it 0750)."""
    return tmp_path / "certs"


@pytest.fixture
def settings(
    make_settings: SettingsFactory, helper_socket: Path, nginx_dir: Path, cert_dir: Path
) -> Settings:
    """App settings pointing at the module helper socket and the tmp website dirs.

    ``make_settings`` has no ``website=`` knob (team-frozen conftest), so the
    website section is merged through model_dump/model_validate.
    """
    base = make_settings(paths={"helper_socket": helper_socket})
    data = base.model_dump()
    data["website"] = {"nginx_dir": str(nginx_dir), "cert_dir": str(cert_dir)}
    return Settings.model_validate(data)


@dataclass
class FakeBins:
    """Fake nginx/systemctl binaries: force ``nginx -t`` / ``reload`` exit codes."""

    tflag: Path
    rflag: Path
    sysargs: Path

    def fail_test(self) -> None:
        self.tflag.write_text("1")

    def systemctl_calls(self) -> list[str]:
        if not self.sysargs.exists():
            return []
        return [line for line in self.sysargs.read_text().splitlines() if line != "----"]


@pytest.fixture
def fake_bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBins:
    """Replace the fixed nginx/systemctl paths with recording fakes (no root needed).

    ``nginx -t`` / ``systemctl reload`` exit with the code stored in a flag file
    (``0`` by default; ``fail_test`` flips the nginx one to ``1``).
    """
    tflag = tmp_path / "tflag"
    rflag = tmp_path / "rflag"
    sysargs = tmp_path / "sysargs.txt"
    tflag.write_text("0")
    rflag.write_text("0")

    nginx_script = tmp_path / "fake-nginx"
    nginx_script.write_text(
        "#!/bin/sh\n"
        f"T={shlex.quote(str(tflag))}\n"
        'case "$*" in\n'
        '  *-t*) echo "nginx: configuration file test" >&2; exit "$(cat "$T")" ;;\n'
        '  *-v*) echo "nginx version: nginx/1.18.0 (Ubuntu)" >&2; exit 0 ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    nginx_script.chmod(0o755)

    systemctl_script = tmp_path / "systemctl"
    systemctl_script.write_text(
        "#!/bin/sh\n"
        f"R={shlex.quote(str(rflag))}\n"
        f"{{ printf '%s\\n' \"$@\"; echo '----'; }} >> {shlex.quote(str(sysargs))}\n"
        'case "$1" in\n'
        "  is-active) echo active ;;\n"
        "  is-enabled) echo enabled ;;\n"
        '  reload) exit "$(cat "$R")" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl_script.chmod(0o755)

    monkeypatch.setattr(nginx, "_NGINX", str(nginx_script))
    monkeypatch.setattr(nginx, "_SYSTEMCTL", str(systemctl_script))
    return FakeBins(tflag=tflag, rflag=rflag, sysargs=sysargs)


# ---------------------------------------------------------------------------
# Helper level: site name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../x", "a/b", "..", ".hidden", "", "a b", "a;b", "foo/", "x\0y", "/etc/passwd"],
)
def test_write_site_rejects_bad_names(helper: HelperClient, nginx_dir: Path, bad: str) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("nginx.write_site", {"nginx_dir": str(nginx_dir), "name": bad, "content": "x"})
    assert excinfo.value.helper_code == HELPER_INVALID_ARGS


def test_read_site_rejects_bad_names(helper: HelperClient, nginx_dir: Path) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("nginx.read_site", {"nginx_dir": str(nginx_dir), "name": "../secret"})
    assert excinfo.value.helper_code == HELPER_INVALID_ARGS


def test_relative_nginx_dir_rejected(helper: HelperClient) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("nginx.list_sites", {"nginx_dir": "relative/nginx"})
    assert excinfo.value.helper_code == HELPER_PATH_INVALID


# ---------------------------------------------------------------------------
# Helper level: site lifecycle
# ---------------------------------------------------------------------------


def test_write_read_roundtrip(helper: HelperClient, nginx_dir: Path) -> None:
    res = helper.call(
        "nginx.write_site",
        {"nginx_dir": str(nginx_dir), "name": "example.com", "content": SAMPLE_SITE},
    )
    assert res == {"name": "example.com", "created": True}
    # File landed in sites-available and is a real file with 0644.
    target = nginx_dir / "sites-available" / "example.com"
    assert target.is_file()
    assert (target.stat().st_mode & 0o777) == 0o644

    read = helper.call("nginx.read_site", {"nginx_dir": str(nginx_dir), "name": "example.com"})
    assert read["content"] == SAMPLE_SITE
    assert read["enabled"] is False

    # Second write overwrites (created=False).
    res2 = helper.call(
        "nginx.write_site",
        {"nginx_dir": str(nginx_dir), "name": "example.com", "content": "server {}\n"},
    )
    assert res2 == {"name": "example.com", "created": False}


def test_read_missing_site_not_found(helper: HelperClient, nginx_dir: Path) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("nginx.read_site", {"nginx_dir": str(nginx_dir), "name": "ghost"})
    assert excinfo.value.helper_code == HELPER_NOT_FOUND


def test_list_sites_parses_and_flags_enabled(helper: HelperClient, nginx_dir: Path) -> None:
    helper.call(
        "nginx.write_site",
        {"nginx_dir": str(nginx_dir), "name": "alpha", "content": SAMPLE_SITE},
    )
    helper.call(
        "nginx.write_site",
        {"nginx_dir": str(nginx_dir), "name": "beta", "content": BETA_SITE},
    )
    helper.call("nginx.enable", {"nginx_dir": str(nginx_dir), "name": "alpha"})

    listing = helper.call("nginx.list_sites", {"nginx_dir": str(nginx_dir)})
    by_name = {site["name"]: site for site in listing["sites"]}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"]["enabled"] is True
    assert by_name["beta"]["enabled"] is False
    assert by_name["alpha"]["server_names"] == ["example.com", "www.example.com"]
    assert by_name["alpha"]["listens"] == ["80"]
    assert by_name["beta"]["listens"] == ["8443"]
    # proxy_pass targets surface as the site's real backend; static sites have none.
    assert by_name["alpha"]["upstreams"] == []
    assert by_name["beta"]["upstreams"] == ["http://127.0.0.1:3000"]


def test_missing_sites_available_lists_empty(helper: HelperClient, nginx_dir: Path) -> None:
    listing = helper.call("nginx.list_sites", {"nginx_dir": str(nginx_dir)})
    assert listing == {"sites": []}


def test_enable_disable_symlink_lifecycle(helper: HelperClient, nginx_dir: Path) -> None:
    helper.call(
        "nginx.write_site",
        {"nginx_dir": str(nginx_dir), "name": "site", "content": SAMPLE_SITE},
    )
    link = nginx_dir / "sites-enabled" / "site"

    res = helper.call("nginx.enable", {"nginx_dir": str(nginx_dir), "name": "site"})
    assert res == {"name": "site", "enabled": True, "changed": True}
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(nginx_dir / "sites-available" / "site")

    # Enable again: idempotent, no change.
    again = helper.call("nginx.enable", {"nginx_dir": str(nginx_dir), "name": "site"})
    assert again["changed"] is False

    off = helper.call("nginx.disable", {"nginx_dir": str(nginx_dir), "name": "site"})
    assert off == {"name": "site", "enabled": False, "changed": True}
    assert not link.exists()

    # Disable again: idempotent, nothing to remove.
    off2 = helper.call("nginx.disable", {"nginx_dir": str(nginx_dir), "name": "site"})
    assert off2["changed"] is False


def test_enable_missing_site_not_found(helper: HelperClient, nginx_dir: Path) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("nginx.enable", {"nginx_dir": str(nginx_dir), "name": "ghost"})
    assert excinfo.value.helper_code == HELPER_NOT_FOUND


def test_delete_site_removes_file_and_symlink(helper: HelperClient, nginx_dir: Path) -> None:
    helper.call(
        "nginx.write_site",
        {"nginx_dir": str(nginx_dir), "name": "gone", "content": SAMPLE_SITE},
    )
    helper.call("nginx.enable", {"nginx_dir": str(nginx_dir), "name": "gone"})

    helper.call("nginx.delete_site", {"nginx_dir": str(nginx_dir), "name": "gone"})
    assert not (nginx_dir / "sites-available" / "gone").exists()
    assert not (nginx_dir / "sites-enabled" / "gone").exists()

    with pytest.raises(HelperActionError) as excinfo:
        helper.call("nginx.delete_site", {"nginx_dir": str(nginx_dir), "name": "gone"})
    assert excinfo.value.helper_code == HELPER_NOT_FOUND


# ---------------------------------------------------------------------------
# Helper level: certificates (real openssl)
# ---------------------------------------------------------------------------


def test_generate_list_and_delete_cert(helper: HelperClient, cert_dir: Path) -> None:
    args = {"cert_dir": str(cert_dir), "name": "example", "domain": "example.com", "days": 30}
    res = helper.call("nginx.generate_self_signed", args)
    assert res["name"] == "example"
    assert "example.com" in res["subject"]

    # cert_dir was created 0750; key is 0600, cert 0644.
    assert (cert_dir.stat().st_mode & 0o777) == 0o750
    assert ((cert_dir / "example.key").stat().st_mode & 0o777) == 0o600
    assert ((cert_dir / "example.crt").stat().st_mode & 0o777) == 0o644

    listing = helper.call("nginx.list_certs", {"cert_dir": str(cert_dir)})
    certs = listing["certs"]
    assert len(certs) == 1
    assert certs[0]["name"] == "example"
    assert certs[0]["file"] == "example.crt"
    assert "example.com" in certs[0]["subject"]
    assert certs[0]["expires"].startswith("20")  # ISO timestamp parsed from openssl
    assert certs[0]["has_key"] is True

    # Regenerating the same name conflicts (no silent clobber).
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("nginx.generate_self_signed", args)
    assert excinfo.value.helper_code == HELPER_CONFLICT

    helper.call("nginx.delete_cert", {"cert_dir": str(cert_dir), "name": "example"})
    assert not (cert_dir / "example.crt").exists()
    assert not (cert_dir / "example.key").exists()

    with pytest.raises(HelperActionError) as excinfo2:
        helper.call("nginx.delete_cert", {"cert_dir": str(cert_dir), "name": "example"})
    assert excinfo2.value.helper_code == HELPER_NOT_FOUND


def test_write_cert_from_pem(
    helper: HelperClient, cert_dir: Path, pem_pair: tuple[str, str]
) -> None:
    cert_pem, key_pem = pem_pair
    helper.call(
        "nginx.write_cert",
        {"cert_dir": str(cert_dir), "name": "uploaded", "cert_pem": cert_pem, "key_pem": key_pem},
    )
    assert (cert_dir / "uploaded.crt").read_text() == cert_pem
    assert ((cert_dir / "uploaded.key").stat().st_mode & 0o777) == 0o600
    listing = helper.call("nginx.list_certs", {"cert_dir": str(cert_dir)})
    assert listing["certs"][0]["subject"].endswith("upload.test")


def test_write_cert_rejects_non_pem(helper: HelperClient, cert_dir: Path) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call(
            "nginx.write_cert",
            {"cert_dir": str(cert_dir), "name": "bad", "cert_pem": "nope", "key_pem": "nope"},
        )
    assert excinfo.value.helper_code == HELPER_INVALID_ARGS


# ---------------------------------------------------------------------------
# API level: auth / CSRF gates
# ---------------------------------------------------------------------------


async def test_list_sites_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{WEBSITE}/sites")
    assert_problem(resp, 401, "auth.required")


async def test_write_site_requires_csrf(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.post(f"{WEBSITE}/sites", json={"name": "x", "content": "server {}\n"})
    assert_problem(resp, 403, "csrf.invalid")


# ---------------------------------------------------------------------------
# API level: site lifecycle + audit
# ---------------------------------------------------------------------------


async def test_create_get_and_list_site(
    client: httpx.AsyncClient, owner: OwnerContext, app: FastAPI, helper: HelperClient
) -> None:
    resp = await client.post(
        f"{WEBSITE}/sites",
        json={"name": "blog", "content": SAMPLE_SITE},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "blog"

    resp = await client.get(f"{WEBSITE}/sites/blog")
    assert resp.status_code == 200
    assert resp.json()["content"] == SAMPLE_SITE

    resp = await client.get(f"{WEBSITE}/sites")
    assert [s["name"] for s in resp.json()["sites"]] == ["blog"]

    # Mutation was audited.
    count = await db_scalar(
        app,
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "website.write_site", AuditLog.result == "ok"),
    )
    assert count == 1


async def test_enable_disable_and_delete_site(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient
) -> None:
    await client.post(
        f"{WEBSITE}/sites",
        json={"name": "shop", "content": SAMPLE_SITE},
        headers=csrf_headers(owner),
    )
    resp = await client.post(f"{WEBSITE}/sites/shop/enable", headers=csrf_headers(owner))
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    resp = await client.get(f"{WEBSITE}/sites")
    assert resp.json()["sites"][0]["enabled"] is True

    resp = await client.post(f"{WEBSITE}/sites/shop/disable", headers=csrf_headers(owner))
    assert resp.json()["enabled"] is False

    resp = await client.request("DELETE", f"{WEBSITE}/sites/shop", headers=csrf_headers(owner))
    assert resp.status_code == 200
    resp = await client.get(f"{WEBSITE}/sites")
    assert resp.json()["sites"] == []


async def test_delete_missing_site_is_404(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient
) -> None:
    resp = await client.request("DELETE", f"{WEBSITE}/sites/ghost", headers=csrf_headers(owner))
    assert_problem(resp, 404, "not_found")


# ---------------------------------------------------------------------------
# API level: test / reload / status via fake binaries
# ---------------------------------------------------------------------------


async def test_reload_tests_then_reloads(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, fake_bins: FakeBins
) -> None:
    resp = await client.post(f"{WEBSITE}/reload", headers=csrf_headers(owner))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["reloaded"] is True
    assert "reload" in fake_bins.systemctl_calls()


async def test_reload_skips_when_test_fails(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, fake_bins: FakeBins
) -> None:
    fake_bins.fail_test()
    resp = await client.post(f"{WEBSITE}/reload", headers=csrf_headers(owner))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["reloaded"] is False
    # nginx -t failed → systemctl reload must NOT have been invoked.
    assert "reload" not in fake_bins.systemctl_calls()


async def test_config_test_endpoint(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, fake_bins: FakeBins
) -> None:
    resp = await client.post(f"{WEBSITE}/test", headers=csrf_headers(owner))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_status_endpoint(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, fake_bins: FakeBins
) -> None:
    resp = await client.get(f"{WEBSITE}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] == "active"
    assert body["enabled"] == "enabled"
    assert "1.18.0" in body["version"]


# ---------------------------------------------------------------------------
# API level: certificates
# ---------------------------------------------------------------------------


async def test_cert_upload_list_delete(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, pem_pair: tuple[str, str]
) -> None:
    cert_pem, key_pem = pem_pair
    resp = await client.post(
        f"{WEBSITE}/certs",
        json={"name": "site1", "cert_pem": cert_pem, "key_pem": key_pem},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"{WEBSITE}/certs")
    certs = resp.json()["certs"]
    assert [c["name"] for c in certs] == ["site1"]
    assert certs[0]["has_key"] is True

    resp = await client.request("DELETE", f"{WEBSITE}/certs/site1", headers=csrf_headers(owner))
    assert resp.status_code == 200
    resp = await client.get(f"{WEBSITE}/certs")
    assert resp.json()["certs"] == []


async def test_self_signed_endpoint(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient
) -> None:
    resp = await client.post(
        f"{WEBSITE}/certs/self-signed",
        json={"name": "selfie", "domain": "selfie.local", "days": 15},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "selfie"

    resp = await client.get(f"{WEBSITE}/certs")
    assert resp.json()["certs"][0]["subject"].endswith("selfie.local")


async def test_upload_cert_rejects_bad_name(
    client: httpx.AsyncClient, owner: OwnerContext, pem_pair: tuple[str, str]
) -> None:
    cert_pem, key_pem = pem_pair
    resp = await client.post(
        f"{WEBSITE}/certs",
        json={"name": "../evil", "cert_pem": cert_pem, "key_pem": key_pem},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 422, "request.validation")
