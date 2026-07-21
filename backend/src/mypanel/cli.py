"""`mypanel` command line interface: serve, db upgrade/revision, bootstrap-token."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import uvicorn

from mypanel.app import create_app
from mypanel.auth.service import AuthService
from mypanel.config import Settings, set_settings
from mypanel.db import make_engine, make_revision, make_sessionmaker, run_migrations
from mypanel.errors import AppError

BOOTSTRAP_TOKEN_TTL_MINUTES = 15


def _load_settings(config: str | None) -> Settings:
    settings = Settings.load(config)
    set_settings(settings)
    return settings


def _cmd_serve(args: argparse.Namespace) -> int:
    settings = _load_settings(args.config)
    settings.validate_for_serve()
    app = create_app(settings)
    ssl_certfile: Path | None = None if settings.server.dev_http else settings.server.tls_cert
    ssl_keyfile: Path | None = None if settings.server.dev_http else settings.server.tls_key
    # log_config=None keeps uvicorn from re-installing its own handlers after
    # setup_logging (create_app) bridged them onto the structlog renderer.
    # proxy_headers=False: the panel terminates TLS itself and never sits behind a
    # proxy, so X-Forwarded-* must never rewrite the peer address (code-plan §5.3);
    # otherwise a loopback client could spoof the audited source IP and the rate-limit
    # key. server_header=False drops the version-disclosing Server header.
    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        log_level="info",
        log_config=None,
        proxy_headers=False,
        server_header=False,
    )
    return 0


def _cmd_db_upgrade(args: argparse.Namespace) -> int:
    settings = _load_settings(args.config)
    run_migrations(settings)
    print(f"database at {settings.database_path} is up to date")
    return 0


def _cmd_db_revision(args: argparse.Namespace) -> int:
    settings = _load_settings(args.config)
    make_revision(settings, args.message, autogenerate=not args.no_autogenerate)
    return 0


async def _issue_bootstrap_token(settings: Settings) -> str:
    engine = make_engine(settings)
    try:
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as db:
            return await AuthService(db, settings).issue_bootstrap_token()
    finally:
        await engine.dispose()


def _cmd_bootstrap_token(args: argparse.Namespace) -> int:
    settings = _load_settings(args.config)
    run_migrations(settings)
    try:
        token = asyncio.run(_issue_bootstrap_token(settings))
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    scheme = "http" if settings.server.dev_http else "https"
    host = settings.server.host
    display_host = "<panel-address>" if host in {"0.0.0.0", "::"} else host
    setup_url = f"{scheme}://{display_host}:{settings.server.port}/setup"
    # The token is printed in the clear exactly once here; it is deliberately not
    # embedded in the URL so it cannot leak into access logs or browser history.
    print(f"Bootstrap token: {token}")
    print(
        f"Valid for {BOOTSTRAP_TOKEN_TTL_MINUTES} minutes. "
        f"Open {setup_url} and paste the token above."
    )
    return 0


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="configuration file (default: $MYPANEL_CONFIG or /etc/mypanel/config.toml)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mypanel", description="mypanel server management panel"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the panel API server")
    _add_config_argument(serve)
    serve.set_defaults(func=_cmd_serve)

    db = sub.add_parser("db", help="database schema management")
    db_sub = db.add_subparsers(dest="db_command", required=True)

    upgrade = db_sub.add_parser("upgrade", help="create or upgrade the database schema")
    _add_config_argument(upgrade)
    upgrade.set_defaults(func=_cmd_db_upgrade)

    revision = db_sub.add_parser("revision", help="generate a new migration (development)")
    revision.add_argument("--message", required=True, help="migration message")
    revision.add_argument(
        "--no-autogenerate",
        action="store_true",
        help="create an empty migration instead of diffing against the database",
    )
    _add_config_argument(revision)
    revision.set_defaults(func=_cmd_db_revision)

    bootstrap = sub.add_parser(
        "bootstrap-token", help="issue a one-time owner setup token (no owner yet)"
    )
    _add_config_argument(bootstrap)
    bootstrap.set_defaults(func=_cmd_bootstrap_token)

    return parser


def main() -> None:
    """Console-script entry point (pyproject: mypanel)."""
    args = _build_parser().parse_args()
    handler: Callable[[argparse.Namespace], int] = args.func
    raise SystemExit(handler(args))
