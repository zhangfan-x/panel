"""/settings endpoints: read-only system configuration view and owner profile.

Writable settings do not exist yet — password/session management reuses the
existing ``/auth/*`` endpoints. The system view deliberately exposes only
non-secret configuration (never ``secret_key``; TLS *paths* are fine).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

import mypanel
from mypanel.auth.deps import ActiveSessionDep, DbDep, SettingsDep
from mypanel.db.models import User
from mypanel.errors import NotFoundError

settings_router = APIRouter(prefix="/settings", tags=["settings"])

# The owner account is pinned to users.id == 1 (DB invariant, see auth service).
_OWNER_USER_ID = 1


def _aware(dt: datetime) -> datetime:
    """Re-attach UTC to a datetime read back from SQLite (stored naive)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@settings_router.get("/system")
async def system_settings(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Read-only view of the effective configuration (no secrets)."""
    return {
        "server": {
            "host": settings.server.host,
            "port": settings.server.port,
            "dev_http": settings.server.dev_http,
            "tls_cert": str(settings.server.tls_cert),
            "tls_key": str(settings.server.tls_key),
        },
        "limits": settings.limits.model_dump(),
        "docker": {"compose_root": str(settings.docker.compose_root)},
        "version": mypanel.__version__,
    }


@settings_router.get("/profile")
async def owner_profile(db: DbDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Owner account facts: username plus creation/password-change timestamps."""
    user = await db.get(User, _OWNER_USER_ID)
    if user is None:
        raise NotFoundError("Owner account does not exist.")
    return {
        "username": user.username,
        "created_at": _aware(user.created_at).isoformat(),
        "password_changed_at": _aware(user.password_changed_at).isoformat(),
    }
