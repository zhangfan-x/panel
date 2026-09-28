"""Task-centre API: list/inspect jobs, follow logs (SSE), cancel and retry."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.responses import StreamingResponse

from ownpanel.audit import record_audit
from ownpanel.auth.deps import (
    ActiveSessionDep,
    CurrentAuth,
    require_csrf,
    resolve_stream_auth,
)
from ownpanel.db.models import JobStatus
from ownpanel.errors import ConflictError, NotFoundError
from ownpanel.events import SSE_HEADERS
from ownpanel.jobs.manager import JobManager

jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])

# Rebuild registry: maps a job ``type`` to a factory that turns stored params
# back into a handler. Domain modules register their retryable job types here.
RETRY_BUILDERS: dict[str, object] = {}


def _manager(request: Request) -> JobManager:
    manager: JobManager = request.app.state.jobs
    return manager


@jobs_router.get("")
async def list_jobs(
    request: Request,
    _auth: ActiveSessionDep,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """List jobs (most recent first), optionally filtered by status."""
    return await _manager(request).list(status=status, limit=limit, offset=offset)


@jobs_router.get("/{job_id}")
async def get_job(request: Request, job_id: str, _auth: ActiveSessionDep) -> dict[str, object]:
    """Fetch a single job's current state."""
    job = await _manager(request).get(job_id)
    if job is None:
        raise NotFoundError("Unknown job.")
    return job


async def _log_source(manager: JobManager, job_id: str, request: Request) -> AsyncIterator[str]:
    async for entry in manager.stream_log(job_id):
        if await request.is_disconnected():
            return
        payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        yield f"event: job-log\ndata: {payload}\n\n"


@jobs_router.get("/{job_id}/log")
async def stream_job_log(
    request: Request,
    job_id: str,
    _auth: Annotated[CurrentAuth, Depends(resolve_stream_auth)],
) -> StreamingResponse:
    """SSE stream of a job's log: full replay then live follow until it ends."""
    manager = _manager(request)
    if await manager.get(job_id) is None:
        raise NotFoundError("Unknown job.")
    return StreamingResponse(
        _log_source(manager, job_id, request),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@jobs_router.post("/{job_id}/cancel", status_code=204, dependencies=[Depends(require_csrf)])
async def cancel_job(request: Request, job_id: str, auth: ActiveSessionDep) -> Response:
    """Request cancellation of a queued or running job."""
    manager = _manager(request)
    await manager.cancel(job_id)
    async with request.app.state.sessionmaker() as db:
        await record_audit(
            db,
            resource="jobs",
            action="jobs.cancel",
            target=job_id,
            actor_user_id=auth.user.id,
            request=request,
            commit=True,
        )
    return Response(status_code=204)


@jobs_router.post("/{job_id}/retry", status_code=202, dependencies=[Depends(require_csrf)])
async def retry_job(request: Request, job_id: str, auth: ActiveSessionDep) -> dict[str, str]:
    """Re-submit a finished job using its stored parameters.

    Only job types whose builder is registered in ``RETRY_BUILDERS`` can retry;
    everything else is a conflict.
    """
    manager = _manager(request)
    job = await manager.get(job_id)
    if job is None:
        raise NotFoundError("Unknown job.")
    if job["status"] not in (JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SUCCEEDED):
        raise ConflictError("Job is not in a retryable state.")
    builder = RETRY_BUILDERS.get(job["type"])
    if builder is None:
        raise ConflictError("This job type does not support retry.")
    # builder(request, stored_params) -> new job_id. Kept as ``object`` in the
    # registry to avoid import cycles; domain modules store a proper callable.
    from typing import Any, cast

    build = cast("Any", builder)
    new_id = await build(request, job)
    async with request.app.state.sessionmaker() as db:
        await record_audit(
            db,
            resource="jobs",
            action="jobs.retry",
            target=job_id,
            actor_user_id=auth.user.id,
            request=request,
            commit=True,
        )
    return {"job_id": new_id}
