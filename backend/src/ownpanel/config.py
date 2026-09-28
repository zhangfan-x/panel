"""Application settings loaded from a TOML file (OWNPANEL_CONFIG or /etc/ownpanel/config.toml)."""

from __future__ import annotations

import ipaddress
import os
import secrets
import stat
import tomllib
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

DEFAULT_CONFIG_PATH = "/etc/ownpanel/config.toml"
SECRET_KEY_FILE = "secret.key"
DB_FILE = "ownpanel.db"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=25443, gt=1024, lt=65536)
    dev_http: bool = False
    tls_cert: Path = Path("/etc/ownpanel/tls/cert.pem")
    tls_key: Path = Path("/etc/ownpanel/tls/key.pem")
    external_hosts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_dev_http_loopback(self) -> ServerConfig:
        if self.dev_http and not _is_loopback(self.host):
            raise ValueError("dev_http=true is only allowed on loopback addresses")
        return self


class PathsConfig(BaseModel):
    data_dir: Path = Path("/opt/ownpanel/data")
    helper_socket: Path = Path("/run/ownpanel/helper.sock")
    terminal_socket: Path = Path("/run/ownpanel/terminal.sock")
    # Built frontend to serve as an SPA; None (dev) leaves static serving to Vite.
    static_dir: Path | None = None


class LimitsConfig(BaseModel):
    session_idle_minutes: int = Field(default=30, gt=0)
    session_max_hours: int = Field(default=12, gt=0)
    reauth_window_seconds: int = Field(default=60, gt=0)
    login_max_attempts: int = Field(default=5, gt=0)
    upload_max_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    job_concurrency: int = Field(default=2, gt=0)
    audit_retention_days: int = Field(default=90, gt=0)
    metric_retention_days: int = Field(default=7, gt=0)
    terminal_idle_minutes: int = Field(default=10, gt=0)
    terminal_max_minutes: int = Field(default=30, gt=0)


class DockerConfig(BaseModel):
    compose_root: Path = Path("/opt/compose")


class WebsiteConfig(BaseModel):
    # nginx lives under root ownership, so all edits go through the helper.
    nginx_dir: Path = Path("/etc/nginx")
    cert_dir: Path = Path("/etc/nginx/ownpanel-certs")


class ProxyConfig(BaseModel):
    # Clash/Mihomo external controller (RESTful API) and its config file. The
    # config file is owned by the API user, so the API edits it directly; the
    # controller is a localhost HTTP API (optionally token-protected).
    clash_api: str = "http://127.0.0.1:9090"
    clash_secret: str = ""
    clash_config: Path | None = None
    # Latency-probe target. The default is Clash's conventional generate_204
    # endpoint; behind a restrictive network it may be unreachable and make
    # every node look dead, so it is overridable (e.g. cp.cloudflare.com).
    # Group tests prefer the group's own `testUrl` when the controller reports one.
    delay_test_url: str = "http://www.gstatic.com/generate_204"
    # The clash core runs as a per-user systemd unit owned by the API user, so
    # on/off is `systemctl --user start|stop|restart <clash_service>` run
    # directly by the API (no root/helper needed).
    clash_service: str = "clash.service"
    # Subscription import: how long to wait for a remote config, and the
    # User-Agent to announce. Most airports switch response format on the UA,
    # so a clash-shaped default is what actually yields a Clash config.
    subscription_timeout_s: float = 30.0
    subscription_user_agent: str = "clash-verge/v2.0.0"
    # Subscriptions are downloaded directly first — that is the fast path and the
    # only one that works on a fresh install with no usable nodes yet. When the
    # direct attempt fails (airport dashboards are routinely blocked on the very
    # network that needs them), the download is retried through this proxy.
    # "auto" discovers the running core's own listen port from the controller;
    # "none"/"" disables the retry; anything else is a proxy URL used verbatim.
    subscription_fallback_proxy: str = "auto"
    # Extend script (QuickJS) sandbox budget. The time limit is enforced by the
    # interpreter's interrupt handler, so a runaway script is always stoppable.
    script_timeout_s: float = 5.0
    script_memory_limit_mb: int = 64
    # How often the background scheduler checks for subscriptions due a refresh.
    subscription_check_interval_s: float = 60.0


class Settings(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    website: WebsiteConfig = Field(default_factory=WebsiteConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        """Load settings from TOML. Missing file yields defaults (dev convenience)."""
        config_path = Path(path or os.environ.get("OWNPANEL_CONFIG", DEFAULT_CONFIG_PATH))
        if config_path.is_file():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
            return cls.model_validate(data)
        return cls()

    @property
    def database_path(self) -> Path:
        return self.paths.data_dir / DB_FILE

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"

    @cached_property
    def secret_key(self) -> bytes:
        """32-byte server secret, created on first use with 0600 permissions."""
        key_path = self.paths.data_dir / SECRET_KEY_FILE
        if key_path.is_file():
            mode = stat.S_IMODE(key_path.stat().st_mode)
            if mode & 0o077:
                raise PermissionError(
                    f"{key_path} must not be group/world accessible (found {oct(mode)})"
                )
            key = key_path.read_bytes()
            if len(key) != 32:
                raise ValueError(f"{key_path} is corrupt: expected 32 bytes, got {len(key)}")
            return key
        self.paths.data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key

    def allowed_hosts(self) -> set[str]:
        """Exact `Host` header values accepted by the panel."""
        hosts = {f"{self.server.host}:{self.server.port}"}
        if _is_loopback(self.server.host):
            hosts.add(f"localhost:{self.server.port}")
            hosts.add(f"127.0.0.1:{self.server.port}")
        hosts.update(self.server.external_hosts)
        return hosts

    def allowed_origins(self) -> set[str]:
        """Exact `Origin` header values accepted for state-changing requests."""
        scheme = "http" if self.server.dev_http else "https"
        origins = {f"{scheme}://{h}" for h in self.allowed_hosts()}
        if self.server.dev_http:
            # Vite dev server origin during development.
            origins.add("http://localhost:5173")
            origins.add("http://127.0.0.1:5173")
        return origins

    def validate_for_serve(self) -> None:
        """Startup checks that must block `ownpanel serve` (production TLS presence)."""
        if self.server.dev_http:
            return
        if not self.server.tls_cert.is_file():
            raise FileNotFoundError(f"TLS certificate not found: {self.server.tls_cert}")
        if not self.server.tls_key.is_file():
            raise FileNotFoundError(f"TLS private key not found: {self.server.tls_key}")


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton (initialised on first access)."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def set_settings(settings: Settings) -> None:
    """Replace the singleton (tests and CLI entry points)."""
    global _settings
    _settings = settings
