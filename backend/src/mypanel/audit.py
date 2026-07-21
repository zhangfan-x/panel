"""Audit trail writer: one row in ``audit_logs`` per security-relevant event."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from mypanel.db.models import AuditLog

_USER_AGENT_MAX = 256


async def record_audit(
    db: AsyncSession,
    *,
    resource: str,
    action: str,
    result: str = "ok",
    target: str = "",
    actor_user_id: int | None = None,
    request: Request | None = None,
    error_code: str | None = None,
    duration_ms: int | None = None,
    commit: bool = False,
) -> None:
    """Append one audit row, taking request metadata from `request` when given.

    With ``commit=False`` the row rides on the caller's next commit; failure
    paths must pass ``commit=True`` so the row survives the request's rollback.
    """
    request_id = ""
    source_ip = ""
    user_agent = ""
    if request is not None:
        request_id = getattr(request.state, "request_id", "")
        if request.client is not None:
            source_ip = request.client.host
        user_agent = request.headers.get("user-agent", "")[:_USER_AGENT_MAX]
    db.add(
        AuditLog(
            event_id=uuid4().hex,
            ts=datetime.now(tz=UTC),
            request_id=request_id,
            actor_user_id=actor_user_id,
            source_ip=source_ip,
            user_agent=user_agent,
            resource=resource,
            action=action,
            target=target,
            result=result,
            error_code=error_code,
            # The column is NOT NULL; events without a measured duration store 0.
            duration_ms=duration_ms if duration_ms is not None else 0,
        )
    )
    if commit:
        await db.commit()
