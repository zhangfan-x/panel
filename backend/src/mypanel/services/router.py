"""Services API: systemd unit list/detail, lifecycle verbs and journal (snapshot + SSE).

Every operation is delegated to the root helper's ``systemd.*`` actions
(docs/code-plan-m2-m5.md §4.3); unit names are re-validated there against a
strict allowlist. The journal follow stream bridges the helper-provided pipe
read end of a ``journalctl -f`` child onto a short-lived SSE response.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from starlette.responses import StreamingResponse

from mypanel.audit import record_audit
from mypanel.auth.deps import (
    ActiveSessionDep,
    CurrentAuth,
    DbDep,
    require_csrf,
    resolve_stream_auth,
)
from mypanel.errors import (
    AppError,
    HelperActionError,
    HelperUnavailableError,
    NotFoundError,
)
from mypanel.events import SSE_HEADERS
from mypanel.ipc.fdpass import close_fds
from mypanel.ipc.messages import HELPER_NOT_FOUND

services_router = APIRouter(prefix="/services", tags=["services"])

# Helper subprocess deadlines are 10s (reads) / 30s (verbs); keep the IPC
# timeout above them so failures surface as helper errors, not client timeouts.
_READ_TIMEOUT_S = 15.0
_ACTION_TIMEOUT_S = 40.0
_STREAM_HEARTBEAT_S = 15.0
_STREAM_BUFFER_MAX = 4 * 1024 * 1024
_JOURNAL_MESSAGE_MAX = 4000

ServiceAction = Literal["start", "stop", "restart", "reload", "enable", "disable"]

UnitPath = Annotated[str, Path(min_length=1, max_length=256)]


async def _helper_systemd(
    request: Request, action: str, args: dict[str, Any], *, timeout_s: float
) -> dict[str, Any]:
    """Run a helper ``systemd.*`` action, mapping an unknown unit to HTTP 404."""
    try:
        result: dict[str, Any] = await request.app.state.helper.acall(
            action, args, timeout_s=timeout_s
        )
    except HelperActionError as exc:
        if exc.helper_code == HELPER_NOT_FOUND:
            raise NotFoundError(exc.detail or "unit not found") from exc
        raise
    return result


@services_router.get("")
async def list_services(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """All service units: loaded state merged with unit-file enabled state."""
    return await _helper_systemd(request, "systemd.list_units", {}, timeout_s=_READ_TIMEOUT_S)


@services_router.get("/{unit}")
async def service_detail(
    unit: UnitPath, request: Request, _auth: ActiveSessionDep
) -> dict[str, Any]:
    """Detailed status of one unit (``systemctl show`` fields)."""
    return await _helper_systemd(
        request, "systemd.status", {"unit": unit}, timeout_s=_READ_TIMEOUT_S
    )


@services_router.post("/{unit}/{action}", dependencies=[Depends(require_csrf)])
async def service_action(
    unit: UnitPath,
    action: ServiceAction,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Run a lifecycle verb on one unit via the root helper (CSRF required)."""
    try:
        await _helper_systemd(
            request, f"systemd.{action}", {"unit": unit}, timeout_s=_ACTION_TIMEOUT_S
        )
    except AppError as exc:
        await record_audit(
            db,
            resource="services",
            action=f"services.{action}",
            result="denied",
            target=unit,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="services",
        action=f"services.{action}",
        target=unit,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"ok": True}


@services_router.get("/{unit}/journal")
async def service_journal(
    unit: UnitPath,
    request: Request,
    _auth: ActiveSessionDep,
    lines: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """Journal snapshot: the last ``lines`` entries, parsed."""
    return await _helper_systemd(
        request,
        "systemd.journal",
        {"unit": unit, "lines": lines, "follow": False},
        timeout_s=_READ_TIMEOUT_S,
    )


def _journal_event(record: dict[str, Any], fallback_unit: str) -> dict[str, Any]:
    """Reduce one raw journalctl JSON record to {ts,message,priority,unit}."""
    message = record.get("MESSAGE")
    if isinstance(message, list):
        # journalctl encodes non-UTF-8 payloads as arrays of byte values.
        try:
            message = bytes(int(byte) & 0xFF for byte in message).decode("utf-8", "replace")
        except (TypeError, ValueError):
            message = ""
    elif not isinstance(message, str):
        message = "" if message is None else str(message)
    ts_raw = record.get("__REALTIME_TIMESTAMP")
    ts = ""
    if isinstance(ts_raw, str) and ts_raw.isdigit():
        ts = datetime.fromtimestamp(int(ts_raw) / 1_000_000, tz=UTC).isoformat()
    priority_raw = record.get("PRIORITY")
    priority = 6
    if isinstance(priority_raw, str) and priority_raw.isdigit():
        priority = int(priority_raw)
    unit = record.get("_SYSTEMD_UNIT") or record.get("UNIT") or fallback_unit
    return {
        "ts": ts,
        "message": message[:_JOURNAL_MESSAGE_MAX],
        "priority": priority,
        "unit": str(unit),
    }


def _sse(topic: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {topic}\ndata: {payload}\n\n"


async def _journal_source(request: Request, fd: int, unit: str) -> AsyncIterator[str]:
    """Yield SSE ``journal`` events from the helper's journalctl pipe fd.

    The pipe fd is switched to non-blocking and watched with the event loop's
    ``add_reader`` (never a blocking thread read: a thread stuck in ``read()``
    could not be cancelled on client disconnect). Closing the fd in ``finally``
    drops the last read end, which makes the journalctl child exit.
    """
    loop = asyncio.get_running_loop()
    os.set_blocking(fd, False)
    readable = asyncio.Event()
    loop.add_reader(fd, readable.set)
    buffer = b""
    try:
        yield _sse("ready", {})
        while True:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                readable.clear()
                try:
                    await asyncio.wait_for(readable.wait(), timeout=_STREAM_HEARTBEAT_S)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    # Heartbeat comment keeps intermediaries from closing us.
                    yield ": ping\n\n"
                continue
            if not chunk:
                return  # journalctl exited (EOF)
            buffer += chunk
            if len(buffer) > _STREAM_BUFFER_MAX:
                buffer = b""  # pathological line; drop rather than grow unbounded
                continue
            while (newline := buffer.find(b"\n")) >= 0:
                line, buffer = buffer[:newline], buffer[newline + 1 :]
                try:
                    record = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield _sse("journal", _journal_event(record, unit))
    finally:
        loop.remove_reader(fd)
        close_fds([fd])


@services_router.get("/{unit}/journal/stream")
async def stream_service_journal(
    unit: UnitPath,
    request: Request,
    _auth: Annotated[CurrentAuth, Depends(resolve_stream_auth)],
    lines: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> StreamingResponse:
    """Live journal follow as a short-lived SSE stream (backlog + new entries)."""
    helper = request.app.state.helper
    try:
        _result, fds = await asyncio.to_thread(
            helper.call_with_fds,
            "systemd.journal",
            {"unit": unit, "lines": lines, "follow": True},
            timeout_s=_READ_TIMEOUT_S,
        )
    except HelperActionError as exc:
        if exc.helper_code == HELPER_NOT_FOUND:
            raise NotFoundError(exc.detail or "unit not found") from exc
        raise
    if len(fds) != 1:
        close_fds(fds)
        raise HelperUnavailableError("journal follow returned no descriptor")
    return StreamingResponse(
        _journal_source(request, fds[0], unit),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
