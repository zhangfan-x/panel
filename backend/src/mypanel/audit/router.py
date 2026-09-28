"""/audit endpoints: read-only queries over the ``audit_logs`` trail.

Import note: this module must not import from ``mypanel.audit`` (the package
``__init__`` imports this router — that would be a cycle); the write side lives
in ``mypanel.audit.service`` and is not needed here because reads are not
themselves audited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, func, select

from mypanel.auth.deps import ActiveSessionDep, DbDep
from mypanel.db.models import AuditLog
from mypanel.errors import NotFoundError

audit_router = APIRouter(prefix="/audit", tags=["audit"])


def _naive_utc(dt: datetime) -> datetime:
    """Bind-parameter form for SQLite comparisons: naive UTC.

    SQLite stores ``DateTime(timezone=True)`` values as naive UTC text and the
    dialect does not convert aware bind parameters, so convert here.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def _aware(dt: datetime) -> datetime:
    """Re-attach UTC to a datetime read back from SQLite (stored naive)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _event_dict(row: AuditLog) -> dict[str, Any]:
    """JSON shape of one audit event (field names are frontend contract)."""
    return {
        "event_id": row.event_id,
        "ts": _aware(row.ts).isoformat(),
        "request_id": row.request_id,
        "actor_user_id": row.actor_user_id,
        "source_ip": row.source_ip,
        "user_agent": row.user_agent,
        "resource": row.resource,
        "action": row.action,
        "target": row.target,
        "result": row.result,
        "error_code": row.error_code,
        "duration_ms": row.duration_ms,
    }


@audit_router.get("")
async def list_audit_events(
    db: DbDep,
    _auth: ActiveSessionDep,
    action: Annotated[str | None, Query()] = None,
    resource: Annotated[str | None, Query()] = None,
    result: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Query the audit trail, newest first, with optional exact/range filters."""
    conds: list[ColumnElement[bool]] = []
    if action is not None:
        conds.append(AuditLog.action == action)
    if resource is not None:
        conds.append(AuditLog.resource == resource)
    if result is not None:
        conds.append(AuditLog.result == result)
    if since is not None:
        conds.append(AuditLog.ts >= _naive_utc(since))
    if until is not None:
        conds.append(AuditLog.ts <= _naive_utc(until))
    total = await db.scalar(select(func.count()).select_from(AuditLog).where(*conds))
    rows = (
        await db.scalars(
            select(AuditLog)
            .where(*conds)
            # id breaks ts ties deterministically (insertion order, newest first).
            .order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {"events": [_event_dict(row) for row in rows], "total": int(total or 0)}


@audit_router.get("/{event_id}")
async def get_audit_event(event_id: str, db: DbDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Fetch a single audit event by its public ``event_id``."""
    row = await db.scalar(select(AuditLog).where(AuditLog.event_id == event_id))
    if row is None:
        raise NotFoundError("Unknown audit event.")
    return _event_dict(row)
