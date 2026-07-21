"""/health (public) and /system/info (authenticated) endpoints."""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from typing import Any

import psutil
from fastapi import APIRouter, Depends

import mypanel
from mypanel.auth.deps import require_active_session

router = APIRouter(tags=["system"])


def _os_release_pretty_name() -> str:
    """PRETTY_NAME from /etc/os-release (or /usr/lib/os-release); "" when absent."""
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except OSError:
        return ""


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe; deliberately exposes nothing beyond the panel version."""
    return {"status": "ok", "version": mypanel.__version__}


@router.get("/system/info", dependencies=[Depends(require_active_session)])
async def system_info() -> dict[str, Any]:
    """Basic host facts for the dashboard."""
    uname = platform.uname()
    return {
        "hostname": uname.node,
        "kernel": uname.release,
        "os_release": _os_release_pretty_name(),
        "arch": uname.machine,
        "uptime_s": int(datetime.now(tz=UTC).timestamp() - psutil.boot_time()),
        "cpu_count": psutil.cpu_count() or 0,
    }
