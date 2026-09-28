"""Engine/session factories and programmatic Alembic entry points.

The async engine is used by the application; migrations always run on a
separate synchronous engine built from the same database path.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from ownpanel.config import Settings

__all__ = ["make_engine", "make_revision", "make_sessionmaker", "run_migrations"]

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# busy_timeout comes first so the journal_mode switch itself cannot fail with SQLITE_BUSY.
_CONNECT_PRAGMAS = (
    "PRAGMA busy_timeout=5000",
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


def _apply_pragmas(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in _CONNECT_PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def make_engine(settings: Settings) -> AsyncEngine:
    """Async SQLite engine with the panel PRAGMAs applied to every new connection."""
    engine = create_async_engine(settings.database_url)
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory for request handlers; commit does not expire loaded objects."""
    return async_sessionmaker(engine, expire_on_commit=False)


def _alembic_config(settings: Settings) -> Config:
    settings.paths.data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    cfg = Config()
    # Values pass through configparser interpolation: literal % must be doubled.
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR).replace("%", "%%"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{settings.database_path}".replace("%", "%%"))
    return cfg


def run_migrations(settings: Settings) -> None:
    """Create or upgrade the database schema to head (synchronous, run before app start)."""
    command.upgrade(_alembic_config(settings), "head")


def make_revision(settings: Settings, message: str, *, autogenerate: bool = True) -> None:
    """Generate a migration in the packaged versions directory (`ownpanel db revision`)."""
    command.revision(_alembic_config(settings), message=message, autogenerate=autogenerate)
