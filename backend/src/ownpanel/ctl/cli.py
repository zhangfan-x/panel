"""`ownpanelctl` command line tool: status checks and owner recovery.

Deliberately talks straight to SQLite via the stdlib driver (no API, no ORM):
it must work while the panel itself is down or its owner is locked out.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ownpanel.config import DEFAULT_CONFIG_PATH, Settings

_SYSTEMCTL = "/usr/bin/systemctl"
_UNITS = ("ownpanel-api", "ownpanel-helper.socket", "ownpanel-terminal.socket")


def _sqlite_timestamp(now: datetime) -> str:
    """Timestamp text in SQLAlchemy's SQLite DATETIME storage format (naive UTC)."""
    return now.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _print_check(name: str, value: str) -> None:
    print(f"{name}: {value}")


def _check_database(settings: Settings) -> tuple[bool, str]:
    """(ok, human-readable status) for the panel database."""
    db_path = settings.database_path
    if not db_path.is_file():
        return False, f"not initialized (missing {db_path}; run: ownpanel db upgrade)"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False, "not initialized (users table missing; run: ownpanel db upgrade)"
        return False, f"FAIL ({exc})"
    except sqlite3.Error as exc:
        return False, f"FAIL ({exc})"
    return True, f"ok ({row[0]} user(s), {db_path})"


def _systemd_states() -> dict[str, str]:
    """ActiveState per unit; "unknown" whenever systemctl is missing or fails."""
    try:
        proc = subprocess.run(
            [_SYSTEMCTL, "is-active", *_UNITS],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return dict.fromkeys(_UNITS, "unknown")
    lines = [line.strip() for line in proc.stdout.splitlines()]
    if len(lines) != len(_UNITS):
        return dict.fromkeys(_UNITS, "unknown")
    return dict(zip(_UNITS, lines, strict=True))


def _check_tls(settings: Settings) -> tuple[bool, str]:
    if settings.server.dev_http:
        return True, "skipped (dev_http)"
    missing = [
        str(path)
        for path in (settings.server.tls_cert, settings.server.tls_key)
        if not path.is_file()
    ]
    if missing:
        return False, f"FAIL (missing {', '.join(missing)})"
    return True, "ok"


def _cmd_status(args: argparse.Namespace) -> int:
    config_path = Path(args.config or os.environ.get("OWNPANEL_CONFIG", DEFAULT_CONFIG_PATH))
    failed = False
    try:
        settings = Settings.load(args.config)
    except Exception as exc:
        _print_check("config", f"FAIL ({exc})")
        _print_check("database", "skipped (config failed)")
        for unit, state in _systemd_states().items():
            _print_check(f"unit {unit}", state)
        _print_check("tls", "skipped (config failed)")
        _print_check("overall", "FAIL")
        return 1

    source = str(config_path) if config_path.is_file() else f"defaults; {config_path} missing"
    _print_check("config", f"ok ({source})")

    db_ok, db_status = _check_database(settings)
    failed |= not db_ok
    _print_check("database", db_status)

    # Unit states are informational: a dev box legitimately runs without systemd.
    for unit, state in _systemd_states().items():
        _print_check(f"unit {unit}", state)

    tls_ok, tls_status = _check_tls(settings)
    failed |= not tls_ok
    _print_check("tls", tls_status)

    _print_check("overall", "FAIL" if failed else "ok")
    return 1 if failed else 0


def _cmd_reset_owner(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        print("error: reset-owner must be run as root", file=sys.stderr)
        return 1
    settings = Settings.load(args.config)
    db_path = settings.database_path
    if not db_path.is_file():
        print(f"error: database not found at {db_path}", file=sys.stderr)
        return 1

    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT id, username FROM users ORDER BY id LIMIT 1").fetchone()
        if row is None:
            print("error: no owner account exists yet", file=sys.stderr)
            return 1
        owner_id, username = int(row[0]), str(row[1])

        if not args.force:
            answer = input(f"Type the owner username ({username!r}) to confirm the reset: ")
            if answer.strip() != username:
                print("aborted: confirmation did not match", file=sys.stderr)
                return 1

        new_password = secrets.token_urlsafe(12)
        # Local import: pulling in Argon2 costs ~0.15 s that `status` must not pay.
        from ownpanel.security.passwords import hash_password

        password_hash = hash_password(new_password)
        now = _sqlite_timestamp(datetime.now(tz=UTC))
        with conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                (password_hash, now, owner_id),
            )
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, owner_id),
            )
            conn.execute(
                "INSERT INTO audit_logs (event_id, ts, request_id, actor_user_id, source_ip,"
                " user_agent, resource, action, target, result, error_code, duration_ms)"
                " VALUES (?, ?, '', NULL, '', '', 'ctl', 'ctl.reset_owner', ?, 'ok', NULL, 0)",
                (uuid.uuid4().hex, now, username),
            )
    finally:
        conn.close()

    print(f"Owner password for {username!r} has been reset.")
    print(f"New password: {new_password}")
    print("All sessions were revoked; log in again with the new password.")
    return 0


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="configuration file (default: $OWNPANEL_CONFIG or /etc/ownpanel/config.toml)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ownpanelctl", description="ownpanel offline administration tool"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="check config, database, systemd units and TLS")
    _add_config_argument(status)
    status.set_defaults(func=_cmd_status)

    reset = sub.add_parser(
        "reset-owner", help="reset the owner password and revoke all sessions (root only)"
    )
    _add_config_argument(reset)
    reset.add_argument("--force", action="store_true", help="skip the interactive confirmation")
    reset.set_defaults(func=_cmd_reset_owner)

    return parser


def main() -> None:
    """Console-script entry point (pyproject: ownpanelctl)."""
    args = _build_parser().parse_args()
    handler: Callable[[argparse.Namespace], int] = args.func
    raise SystemExit(handler(args))
