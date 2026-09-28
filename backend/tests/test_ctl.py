"""Tests for `ownpanelctl` (ownpanel.ctl.cli): offline status checks and owner recovery.

No API app is started here (code-plan section 7: the tool must work while the
panel is down). Each test writes a real config TOML under ``tmp_path``, creates
the schema with ``run_migrations``, seeds rows through the stdlib sqlite3
driver, and drives the CLI through the same argparse entry point the
``ownpanelctl`` console script uses.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ownpanel.auth.service import AuthService
from ownpanel.config import Settings
from ownpanel.ctl import cli as ctl
from ownpanel.db import make_engine, make_sessionmaker, run_migrations
from ownpanel.errors import InvalidCredentialsError
from ownpanel.security.passwords import hash_password, verify_password

OWNER = "owner"
OLD_PASSWORD = "OldPassword123!"
# One Argon2 pass at import time, shared by every test that seeds the owner.
OLD_PASSWORD_HASH = hash_password(OLD_PASSWORD)

UNITS = ("ownpanel-api", "ownpanel-helper.socket", "ownpanel-terminal.socket")


def _ts(dt: datetime) -> str:
    """SQLAlchemy's SQLite DATETIME storage format: naive UTC, space separator."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _parse_ts(text: str) -> datetime:
    """Parse a stored timestamp back into an aware UTC datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@dataclass
class CtlEnv:
    config_path: Path
    settings: Settings
    db_path: Path


def make_env(tmp_path: Path, *, migrate: bool = True) -> CtlEnv:
    """Write a dev-http loopback config rooted in tmp_path; optionally build the schema."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 25443\n"
        "dev_http = true\n"
        "\n"
        "[paths]\n"
        f"data_dir = {json.dumps(str(tmp_path / 'data'))}\n"
        f"helper_socket = {json.dumps(str(tmp_path / 'helper.sock'))}\n"
        f"terminal_socket = {json.dumps(str(tmp_path / 'terminal.sock'))}\n",
        encoding="utf-8",
    )
    settings = Settings.load(config_path)
    if migrate:
        run_migrations(settings)
    return CtlEnv(config_path=config_path, settings=settings, db_path=settings.database_path)


@pytest.fixture
def env(tmp_path: Path) -> CtlEnv:
    return make_env(tmp_path)


def seed_owner(db_path: Path) -> None:
    """Insert the owner (known Argon2 hash) and two live sessions."""
    now = datetime.now(tz=UTC)
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, created_at, password_changed_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    OWNER,
                    OLD_PASSWORD_HASH,
                    _ts(now - timedelta(days=30)),
                    _ts(now - timedelta(days=30)),
                ),
            )
            owner_id = cursor.lastrowid
            assert owner_id is not None
            for n in (1, 2):
                conn.execute(
                    "INSERT INTO sessions (token_hash, user_id, state, created_at, last_seen_at,"
                    " absolute_expires_at, reauth_at, revoked_at, ip, user_agent)"
                    " VALUES (?, ?, 'active', ?, ?, ?, NULL, NULL, '127.0.0.1', 'pytest')",
                    (
                        f"{n:064x}",
                        owner_id,
                        _ts(now - timedelta(hours=1)),
                        _ts(now),
                        _ts(now + timedelta(hours=11)),
                    ),
                )
    finally:
        conn.close()


def fetch_rows(db_path: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    """Run a read-only query against the ctl database and return all rows."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def fetch_one(db_path: Path, sql: str, *params: object) -> tuple[Any, ...]:
    rows = fetch_rows(db_path, sql, *params)
    assert len(rows) == 1, f"expected exactly one row for {sql!r}, got {rows!r}"
    return rows[0]


def db_snapshot(db_path: Path) -> tuple[Any, ...]:
    """State fingerprint used by 'nothing changed' assertions."""
    return (
        *fetch_one(db_path, "SELECT password_hash, password_changed_at FROM users"),
        fetch_one(db_path, "SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL")[0],
        fetch_one(db_path, "SELECT COUNT(*) FROM audit_logs")[0],
    )


def run_ctl(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Invoke the real console-script entry point; return the process exit code."""
    monkeypatch.setattr(sys, "argv", ["ownpanelctl", *argv])
    with pytest.raises(SystemExit) as excinfo:
        ctl.main()
    code = excinfo.value.code
    assert isinstance(code, int), f"ownpanelctl exited with a non-integer status: {code!r}"
    return code


def extract_new_password(out: str) -> str:
    match = re.search(r"^New password: (\S+)$", out, re.MULTILINE)
    assert match, f"no 'New password:' line in output:\n{out}"
    return match.group(1)


class TestResetOwner:
    def test_force_rotates_password_and_revokes_everything(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed_owner(env.db_path)
        seeded = fetch_one(env.db_path, "SELECT password_changed_at FROM users")[0]
        changed_before = _parse_ts(seeded)
        monkeypatch.setattr("os.geteuid", lambda: 0)

        code = run_ctl(monkeypatch, "reset-owner", "--config", str(env.config_path), "--force")

        assert code == 0
        out = capsys.readouterr().out
        assert f"Owner password for {OWNER!r} has been reset." in out
        new_password = extract_new_password(out)

        new_hash, changed_at = fetch_one(
            env.db_path,
            "SELECT password_hash, password_changed_at FROM users WHERE username = ?",
            OWNER,
        )
        assert new_hash != OLD_PASSWORD_HASH
        assert verify_password(new_password, new_hash)
        assert not verify_password(OLD_PASSWORD, new_hash)
        assert _parse_ts(changed_at) > changed_before

        assert fetch_one(env.db_path, "SELECT COUNT(*) FROM sessions")[0] == 2
        revoked = fetch_rows(env.db_path, "SELECT revoked_at FROM sessions")
        assert all(row[0] is not None for row in revoked)

        actor, resource, action, target, result = fetch_one(
            env.db_path,
            "SELECT actor_user_id, resource, action, target, result FROM audit_logs",
        )
        assert actor is None
        assert resource == "ctl"
        assert action == "ctl.reset_owner"
        assert target == OWNER
        assert result == "ok"

    def test_refused_without_root(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed_owner(env.db_path)
        before = db_snapshot(env.db_path)
        monkeypatch.setattr("os.geteuid", lambda: 1000)

        code = run_ctl(monkeypatch, "reset-owner", "--config", str(env.config_path), "--force")

        assert code == 1
        assert "must be run as root" in capsys.readouterr().err
        assert db_snapshot(env.db_path) == before

    def test_interactive_mismatch_aborts(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed_owner(env.db_path)
        before = db_snapshot(env.db_path)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr("builtins.input", lambda _prompt: "not-the-owner")

        code = run_ctl(monkeypatch, "reset-owner", "--config", str(env.config_path))

        assert code == 1
        assert "confirmation did not match" in capsys.readouterr().err
        assert db_snapshot(env.db_path) == before

    def test_interactive_confirmation_accepts_username(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed_owner(env.db_path)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr("builtins.input", lambda _prompt: f"  {OWNER}  ")

        code = run_ctl(monkeypatch, "reset-owner", "--config", str(env.config_path))

        assert code == 0
        new_password = extract_new_password(capsys.readouterr().out)
        (new_hash,) = fetch_one(env.db_path, "SELECT password_hash FROM users")
        assert verify_password(new_password, new_hash)

    def test_missing_database_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = make_env(tmp_path, migrate=False)
        monkeypatch.setattr("os.geteuid", lambda: 0)

        code = run_ctl(monkeypatch, "reset-owner", "--config", str(env.config_path), "--force")

        assert code == 1
        assert "database not found" in capsys.readouterr().err

    def test_empty_database_reports_no_owner(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("os.geteuid", lambda: 0)

        code = run_ctl(monkeypatch, "reset-owner", "--config", str(env.config_path), "--force")

        assert code == 1
        assert "no owner account" in capsys.readouterr().err

    async def test_new_password_logs_in(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Recovery closure: after reset the old password is dead and the new one
        opens an ``active`` session."""
        seed_owner(env.db_path)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        code = run_ctl(monkeypatch, "reset-owner", "--config", str(env.config_path), "--force")
        assert code == 0
        new_password = extract_new_password(capsys.readouterr().out)

        engine = make_engine(env.settings)
        try:
            sessionmaker = make_sessionmaker(engine)
            async with sessionmaker() as db:
                with pytest.raises(InvalidCredentialsError):
                    await AuthService(db, env.settings).login(
                        OWNER, OLD_PASSWORD, ip="127.0.0.1", user_agent="pytest"
                    )
            async with sessionmaker() as db:
                session, raw_token = await AuthService(db, env.settings).login(
                    OWNER, new_password, ip="127.0.0.1", user_agent="pytest"
                )
            assert session.state == "active"
            assert raw_token
        finally:
            await engine.dispose()


class TestStatus:
    def test_healthy_dev_setup_exits_zero(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed_owner(env.db_path)

        code = run_ctl(monkeypatch, "status", "--config", str(env.config_path))

        assert code == 0
        out = capsys.readouterr().out
        assert f"config: ok ({env.config_path})" in out
        assert re.search(r"^database: ok \(1 user\(s\), .*ownpanel\.db\)$", out, re.MULTILINE)
        assert "tls: skipped (dev_http)" in out
        for unit in UNITS:
            assert re.search(rf"^unit {re.escape(unit)}: \S+", out, re.MULTILINE)
        assert "overall: ok" in out

    def test_units_unknown_without_systemctl_is_informational(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No systemd on the box: units show 'unknown' but status still passes."""
        monkeypatch.setattr(ctl, "_SYSTEMCTL", str(env.config_path.parent / "no-such-systemctl"))

        code = run_ctl(monkeypatch, "status", "--config", str(env.config_path))

        assert code == 0
        out = capsys.readouterr().out
        for unit in UNITS:
            assert f"unit {unit}: unknown" in out
        assert "overall: ok" in out

    def test_missing_database_fails(
        self, env: CtlEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.db_path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(env.db_path) + suffix).unlink(missing_ok=True)

        code = run_ctl(monkeypatch, "status", "--config", str(env.config_path))

        assert code == 1
        out = capsys.readouterr().out
        assert "config: ok" in out
        assert "database: not initialized (missing" in out
        assert "overall: FAIL" in out

    def test_uninitialized_database_file_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = make_env(tmp_path, migrate=False)
        env.db_path.parent.mkdir(parents=True, exist_ok=True)
        env.db_path.touch()

        code = run_ctl(monkeypatch, "status", "--config", str(env.config_path))

        assert code == 1
        out = capsys.readouterr().out
        assert "database: not initialized (users table missing" in out
        assert "overall: FAIL" in out

    def test_corrupt_config_toml_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[server\nhost = ???\n", encoding="utf-8")

        code = run_ctl(monkeypatch, "status", "--config", str(config_path))

        assert code == 1
        out = capsys.readouterr().out
        assert "config: FAIL (" in out
        assert "database: skipped (config failed)" in out
        assert "overall: FAIL" in out

    def test_config_failing_validation_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Parses as TOML but violates the model: dev_http on a non-loopback host."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\nhost = "0.0.0.0"\ndev_http = true\n', encoding="utf-8")

        code = run_ctl(monkeypatch, "status", "--config", str(config_path))

        assert code == 1
        out = capsys.readouterr().out
        assert "config: FAIL (" in out
        assert "overall: FAIL" in out

    def test_missing_tls_files_fail_in_production_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_env(tmp_path)  # migrated database under tmp_path/data
        config_path = tmp_path / "prod.toml"
        config_path.write_text(
            "[server]\n"
            'host = "127.0.0.1"\n'
            "dev_http = false\n"
            f"tls_cert = {json.dumps(str(tmp_path / 'missing-cert.pem'))}\n"
            f"tls_key = {json.dumps(str(tmp_path / 'missing-key.pem'))}\n"
            "\n"
            "[paths]\n"
            f"data_dir = {json.dumps(str(tmp_path / 'data'))}\n",
            encoding="utf-8",
        )

        code = run_ctl(monkeypatch, "status", "--config", str(config_path))

        assert code == 1
        out = capsys.readouterr().out
        assert "database: ok" in out
        assert "tls: FAIL (missing" in out
        assert "overall: FAIL" in out
