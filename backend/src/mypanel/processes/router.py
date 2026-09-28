"""Processes API: list/detail via API-side psutil; signal/renice via the root helper.

``/proc`` is world-readable, so listing needs no privilege (docs/code-plan-m2-m5.md
§0.2); only the mutating endpoints go through the helper. Response field names
are frontend contract; do not rename them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import psutil
from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from mypanel.audit import record_audit
from mypanel.auth.deps import ActiveSessionDep, DbDep, require_csrf, require_recent_reauth
from mypanel.errors import AppError, ForbiddenError, HelperActionError, NotFoundError
from mypanel.ipc.messages import HELPER_NOT_FOUND

processes_router = APIRouter(prefix="/processes", tags=["processes"])

_HELPER_TIMEOUT_S = 10.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _list_row(proc: psutil.Process) -> dict[str, Any] | None:
    """One list entry, or None when the process vanished or denies access."""
    try:
        with proc.oneshot():
            return {
                "pid": proc.pid,
                "ppid": proc.ppid(),
                "user": proc.username(),
                "cpu": round(proc.cpu_percent(interval=None), 2),
                "mem": round(proc.memory_percent(), 2),
                "status": proc.status(),
                "started": _iso(proc.create_time()),
                "name": proc.name(),
                "cmdline": " ".join(proc.cmdline()),
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _collect_rows() -> list[dict[str, Any]]:
    """Snapshot every visible process (blocking; run via ``asyncio.to_thread``).

    ``process_iter`` caches ``Process`` instances, so ``cpu_percent`` deltas
    accumulate between successive list calls (the first call reports 0.0).
    """
    return [row for proc in psutil.process_iter() if (row := _list_row(proc)) is not None]


@processes_router.get("")
async def list_processes(
    _auth: ActiveSessionDep,
    search: Annotated[str, Query(max_length=256)] = "",
    sort: Annotated[Literal["pid", "cpu", "mem", "name"], Query()] = "cpu",
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Process list with name/cmdline substring search, sort and pagination."""
    rows = await asyncio.to_thread(_collect_rows)
    if search:
        needle = search.lower()
        rows = [
            row
            for row in rows
            if needle in str(row["name"]).lower() or needle in str(row["cmdline"]).lower()
        ]
    total = len(rows)
    reverse = order == "desc"
    if sort == "name":
        rows.sort(key=lambda row: str(row["name"]).lower(), reverse=reverse)
    else:
        rows.sort(key=lambda row: float(row[sort]), reverse=reverse)
    return {"processes": rows[offset : offset + limit], "total": total}


def _read_str(getter: Callable[[], str]) -> str:
    """Best-effort string attribute: '' where /proc denies us (exe/cwd of others)."""
    try:
        return getter()
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return ""


def _detail(pid: int) -> dict[str, Any]:
    """Single-process detail (blocking; run via ``asyncio.to_thread``)."""
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            mem = proc.memory_info()
            row: dict[str, Any] = {
                "pid": proc.pid,
                "ppid": proc.ppid(),
                "user": proc.username(),
                "cpu": round(proc.cpu_percent(interval=None), 2),
                "mem": round(proc.memory_percent(), 2),
                "status": proc.status(),
                "started": _iso(proc.create_time()),
                "name": proc.name(),
                "cmdline": " ".join(proc.cmdline()),
                "nice": proc.nice(),
                "num_threads": proc.num_threads(),
                "rss": mem.rss,
                "vms": mem.vms,
            }
        row["exe"] = _read_str(proc.exe)
        row["cwd"] = _read_str(proc.cwd)
        row["children"] = [child.pid for child in proc.children()]
    except psutil.NoSuchProcess as exc:
        raise NotFoundError(f"process {pid} not found") from exc
    except psutil.AccessDenied as exc:
        raise ForbiddenError(f"process {pid} is not accessible") from exc
    return row


@processes_router.get("/{pid}")
async def process_detail(
    pid: Annotated[int, Path(ge=1)],
    _auth: ActiveSessionDep,
) -> dict[str, Any]:
    """Detail for one process, including its direct children's pids."""
    return await asyncio.to_thread(_detail, pid)


class SignalRequest(BaseModel):
    signal: Literal["SIGTERM", "SIGKILL"]


class ReniceRequest(BaseModel):
    nice: int = Field(ge=-20, le=19)


async def _helper_process_call(
    request: Request, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Run a helper ``process.*`` action, mapping a stale pid to HTTP 404."""
    try:
        result: dict[str, Any] = await request.app.state.helper.acall(
            action, args, timeout_s=_HELPER_TIMEOUT_S
        )
    except HelperActionError as exc:
        if exc.helper_code == HELPER_NOT_FOUND:
            raise NotFoundError(exc.detail or "process not found") from exc
        raise
    return result


@processes_router.post(
    "/{pid}/signal",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def signal_process(
    pid: Annotated[int, Path(ge=1)],
    body: SignalRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Send SIGTERM/SIGKILL via the root helper (recent re-auth required)."""
    target = f"{pid}:{body.signal}"
    try:
        await _helper_process_call(request, "process.signal", {"pid": pid, "signal": body.signal})
    except AppError as exc:
        await record_audit(
            db,
            resource="processes",
            action="processes.signal",
            result="denied",
            target=target,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="processes",
        action="processes.signal",
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"ok": True}


@processes_router.post("/{pid}/renice", dependencies=[Depends(require_csrf)])
async def renice_process(
    pid: Annotated[int, Path(ge=1)],
    body: ReniceRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Change a process's nice value (-20..19) via the root helper."""
    target = f"{pid}:{body.nice}"
    try:
        result = await _helper_process_call(
            request, "process.renice", {"pid": pid, "nice": body.nice}
        )
    except AppError as exc:
        await record_audit(
            db,
            resource="processes",
            action="processes.renice",
            result="denied",
            target=target,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="processes",
        action="processes.renice",
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"nice": result.get("nice", body.nice)}
