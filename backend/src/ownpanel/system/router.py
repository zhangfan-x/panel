"""/health (public), /system/info, /system/overview and /system/metrics."""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from typing import Annotated, Any

import psutil
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import ownpanel
from ownpanel.auth.deps import ActiveSessionDep, get_db, require_active_session
from ownpanel.db.models import AuditLog, Job
from ownpanel.errors import AppError
from ownpanel.metrics.sampler import MetricsSampler

router = APIRouter(tags=["system"])


def _os_release_pretty_name() -> str:
    """PRETTY_NAME from /etc/os-release (or /usr/lib/os-release); "" when absent."""
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except OSError:
        return ""


def _system_info() -> dict[str, Any]:
    uname = platform.uname()
    return {
        "hostname": uname.node,
        "kernel": uname.release,
        "os_release": _os_release_pretty_name(),
        "arch": uname.machine,
        "uptime_s": int(datetime.now(tz=UTC).timestamp() - psutil.boot_time()),
        "cpu_count": psutil.cpu_count() or 0,
    }


def _metrics(request: Request) -> MetricsSampler:
    sampler: MetricsSampler = request.app.state.metrics
    return sampler


def _downsample(samples: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    """Evenly thin *samples* down to at most *max_points* entries.

    Stride sampling: keep every ``ceil(n / max_points)``-th sample starting from
    the oldest, then force-include the newest one so the chart's right edge is
    always the latest data. Returns the input unchanged when it already fits.
    """
    total = len(samples)
    if total <= max_points:
        return samples
    stride = -(-total // max_points)  # ceil(total / max_points)
    thinned = samples[::stride]
    if thinned[-1] is not samples[-1]:
        if len(thinned) < max_points:
            thinned.append(samples[-1])
        else:
            thinned[-1] = samples[-1]
    return thinned


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe; deliberately exposes nothing beyond the panel version."""
    return {"status": "ok", "version": ownpanel.__version__}


@router.get("/system/info", dependencies=[Depends(require_active_session)])
async def system_info() -> dict[str, Any]:
    """Basic host facts for the dashboard."""
    return _system_info()


async def _docker_summary(request: Request) -> dict[str, Any]:
    """Best-effort docker daemon summary; degrades to unavailable on any error."""
    try:
        info = await request.app.state.helper.acall("docker.info", {}, timeout_s=5.0)
    except AppError:
        return {"available": False, "running": 0, "total": 0}
    if not info.get("available", True):
        return {"available": False, "running": 0, "total": 0}
    return {
        "available": True,
        "running": int(info.get("running", 0)),
        "total": int(info.get("containers", 0)),
    }


async def _systemd_summary(request: Request) -> dict[str, Any]:
    """Best-effort count of failed systemd units."""
    try:
        result = await request.app.state.helper.acall("systemd.list_units", {}, timeout_s=5.0)
    except AppError:
        return {"failed": 0, "available": False}
    units = result.get("units", [])
    failed = sum(1 for u in units if u.get("active") == "failed")
    return {"failed": failed, "available": True, "total": len(units)}


@router.get("/system/overview")
async def system_overview(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _auth: ActiveSessionDep,
) -> dict[str, Any]:
    """Aggregated dashboard payload: host facts, live metrics, service summaries."""
    recent_jobs = (await db.scalars(select(Job).order_by(Job.created_at.desc()).limit(8))).all()
    security_events = (
        await db.scalars(
            select(AuditLog).where(AuditLog.result != "ok").order_by(AuditLog.ts.desc()).limit(8)
        )
    ).all()
    return {
        "host": _system_info(),
        "metrics": _metrics(request).latest(),
        "docker": await _docker_summary(request),
        "systemd": await _systemd_summary(request),
        "recent_jobs": [
            {
                "id": j.id,
                "type": j.type,
                "title": j.title,
                "status": j.status,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in recent_jobs
        ],
        "security_events": [
            {
                "event_id": e.event_id,
                "ts": e.ts.isoformat() if e.ts else None,
                "action": e.action,
                "resource": e.resource,
                "result": e.result,
                "error_code": e.error_code,
                "source_ip": e.source_ip,
            }
            for e in security_events
        ],
    }


@router.get("/system/metrics")
async def system_metrics(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _auth: ActiveSessionDep,
    minutes: Annotated[int, Query(ge=1, le=10080)] = 1440,
    max_points: Annotated[int, Query(ge=100, le=10080)] = 2000,
) -> dict[str, Any]:
    """Metric samples for charts over the requested window (default 24h).

    Short windows (<=20 min) use the 5s live ring buffer for high resolution;
    longer windows come from the per-minute ``metric_samples`` history, thinned
    to at most ``max_points`` samples to bound the response size. Falls back to
    the live ring when nothing has been persisted yet (fresh start).
    """
    sampler = _metrics(request)
    if minutes <= 20:
        live = sampler.recent(limit=240)
        if live:
            return {"samples": live, "source": "live"}
    history = await sampler.history(db, minutes=minutes)
    if history:
        return {"samples": _downsample(history, max_points), "source": "history"}
    return {"samples": sampler.recent(limit=240), "source": "live"}
