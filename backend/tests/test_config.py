"""Settings: dev_http loopback enforcement, host/origin allow-lists, secret key, load().

Covers docs/code-plan.md section 2 (config constraints and server secret handling).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from mypanel.config import SECRET_KEY_FILE, Settings

# --- dev_http / server validation -------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::", "8.8.8.8"])
def test_dev_http_rejects_non_loopback_host(make_settings, host):
    with pytest.raises(ValidationError, match="loopback"):
        make_settings(host=host, dev_http=True)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "localhost", "::1"])
def test_dev_http_accepts_loopback_host(make_settings, host):
    settings = make_settings(host=host, dev_http=True)
    assert settings.server.host == host
    assert settings.server.dev_http is True


def test_non_dev_allows_any_host(make_settings):
    settings = make_settings(dev_http=False, host="0.0.0.0")
    assert settings.server.dev_http is False
    assert settings.server.host == "0.0.0.0"


@pytest.mark.parametrize("port", [0, 80, 1024, 65536])
def test_port_bounds_enforced(make_settings, port):
    with pytest.raises(ValidationError):
        make_settings(dev_http=False, host="0.0.0.0", port=port)


# --- allowed hosts / origins -------------------------------------------------------


def test_allowed_hosts_loopback_includes_local_forms(make_settings):
    settings = make_settings()  # 127.0.0.1:25443
    assert settings.allowed_hosts() == {"127.0.0.1:25443", "localhost:25443"}


def test_allowed_hosts_for_localhost_host(make_settings):
    settings = make_settings(host="localhost", port=30001)
    assert settings.allowed_hosts() == {"localhost:30001", "127.0.0.1:30001"}


def test_allowed_hosts_non_loopback_with_external_hosts(make_settings):
    settings = make_settings(
        dev_http=False, host="0.0.0.0", server={"external_hosts": ["192.168.1.10:25443"]}
    )
    assert settings.allowed_hosts() == {"0.0.0.0:25443", "192.168.1.10:25443"}


def test_allowed_origins_dev_http_uses_http_and_adds_vite(make_settings):
    settings = make_settings()
    assert settings.allowed_origins() == {
        "http://127.0.0.1:25443",
        "http://localhost:25443",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }


def test_allowed_origins_production_https_only(make_settings):
    settings = make_settings(
        dev_http=False, host="0.0.0.0", server={"external_hosts": ["panel.lan:25443"]}
    )
    assert settings.allowed_origins() == {
        "https://0.0.0.0:25443",
        "https://panel.lan:25443",
    }


# --- secret key management ---------------------------------------------------------


def _prewrite_key(settings: Settings, data: bytes, mode: int) -> Path:
    settings.paths.data_dir.mkdir(parents=True, exist_ok=True)
    key_file = settings.paths.data_dir / SECRET_KEY_FILE
    key_file.write_bytes(data)
    key_file.chmod(mode)
    return key_file


def test_secret_key_created_with_0600_and_read_back_identically(make_settings, tmp_path):
    shared = tmp_path / "shared-key-dir"
    first = make_settings(base_dir=shared)
    key = first.secret_key
    assert isinstance(key, bytes)
    assert len(key) == 32
    key_file = first.paths.data_dir / SECRET_KEY_FILE
    assert key_file.read_bytes() == key
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    # A second Settings instance over the same data_dir reads the same key back.
    second = make_settings(base_dir=shared)
    assert second.secret_key == key


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_secret_key_rejects_group_or_world_accessible_file(make_settings, mode):
    settings = make_settings()
    _prewrite_key(settings, b"k" * 32, mode)
    with pytest.raises(PermissionError):
        _ = settings.secret_key


@pytest.mark.parametrize("size", [0, 16, 31, 33])
def test_secret_key_rejects_wrong_length(make_settings, size):
    settings = make_settings()
    _prewrite_key(settings, b"k" * size, 0o600)
    with pytest.raises(ValueError, match="32 bytes"):
        _ = settings.secret_key


# --- validate_for_serve ------------------------------------------------------------


def test_validate_for_serve_skips_tls_checks_in_dev_http(make_settings):
    settings = make_settings()  # default /etc TLS paths do not exist in tests
    assert not settings.server.tls_cert.is_file()
    settings.validate_for_serve()  # must not raise


def test_validate_for_serve_requires_tls_files_in_production(make_settings, tmp_path):
    cert = tmp_path / "tls" / "cert.pem"
    key = tmp_path / "tls" / "key.pem"
    settings = make_settings(
        dev_http=False, host="0.0.0.0", server={"tls_cert": cert, "tls_key": key}
    )
    with pytest.raises(FileNotFoundError, match="certificate"):
        settings.validate_for_serve()
    cert.parent.mkdir(parents=True)
    cert.write_text("cert")
    with pytest.raises(FileNotFoundError, match="private key"):
        settings.validate_for_serve()
    key.write_text("key")
    settings.validate_for_serve()  # both present: no error


# --- Settings.load -----------------------------------------------------------------

_TOML = """\
[server]
host = "127.0.0.1"
port = 30001
dev_http = true
external_hosts = ["box.lan:30001"]

[paths]
data_dir = '{data_dir}'

[limits]
session_idle_minutes = 5
login_max_attempts = 3

[docker]
compose_root = '{compose_root}'
"""


def _write_toml(tmp_path: Path, name: str = "config.toml") -> Path:
    cfg = tmp_path / name
    cfg.write_text(_TOML.format(data_dir=tmp_path / "data", compose_root=tmp_path / "compose"))
    return cfg


def test_load_missing_file_yields_defaults(tmp_path):
    settings = Settings.load(tmp_path / "does-not-exist.toml")
    assert settings.server.host == "0.0.0.0"
    assert settings.server.port == 25443
    assert settings.server.dev_http is False
    assert settings.paths.data_dir == Path("/opt/mypanel/data")
    assert settings.limits.session_idle_minutes == 30
    assert settings.limits.login_max_attempts == 5
    assert settings.docker.compose_root == Path("/opt/compose")


def test_load_parses_toml_file(tmp_path):
    settings = Settings.load(_write_toml(tmp_path))
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 30001
    assert settings.server.dev_http is True
    assert settings.server.external_hosts == ["box.lan:30001"]
    assert settings.paths.data_dir == tmp_path / "data"
    assert settings.limits.session_idle_minutes == 5
    assert settings.limits.login_max_attempts == 3
    assert settings.limits.session_max_hours == 12  # untouched keys keep defaults
    assert settings.docker.compose_root == tmp_path / "compose"
    assert settings.database_path == tmp_path / "data" / "mypanel.db"
    assert settings.database_url == f"sqlite+aiosqlite:///{tmp_path / 'data' / 'mypanel.db'}"


def test_load_honours_mypanel_config_env_var(tmp_path, monkeypatch):
    cfg = _write_toml(tmp_path, "env-config.toml")
    monkeypatch.setenv("MYPANEL_CONFIG", str(cfg))
    settings = Settings.load()
    assert settings.server.port == 30001


def test_load_env_var_pointing_at_missing_file_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("MYPANEL_CONFIG", str(tmp_path / "gone.toml"))
    settings = Settings.load()
    assert settings.server.port == 25443
    assert settings.server.dev_http is False
