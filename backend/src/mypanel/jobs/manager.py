"""In-process background job queue with SQLite persistence (code-plan-m2-m5 §2.2).

The manager owns an asyncio worker pool bounded by ``limits.job_concurrency``.
Jobs are submitted with a coroutine handler; state transitions
(``queued → running → succeeded|failed|cancelled``) and milestone log lines are
persisted to ``jobs``/``job_events`` and published to the event bus. Full log
bodies are appended to ``data_dir/jobs/<id>.log`` (not the DB).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mypanel.config import Settings
from mypanel.db.models import Job, JobEvent, JobStatus
from mypanel.events import EventBus
from mypanel.jobs.context import JobCancelled, JobContext
from mypanel.logging import get_logger

log = get_logger(__name__)

JobHandler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]

# Cap milestone JobEvent rows so a chatty job cannot bloat the table; full logs
# live in the per-job log file.
_MAX_JOB_EVENTS = 500

# Once a job reaches one of these it must never transition again.
_TERMINAL_STATUSES = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "title": job.title,
        "status": job.status,
        "progress": job.progress,
        "stage": job.stage,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "resource_key": job.resource_key,
    }


class JobManager:
    """Owns the job worker pool, persistence and per-job coordination state."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        events: EventBus,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._events = events
        self._log_dir = settings.paths.data_dir / "jobs"
        self._semaphore = asyncio.Semaphore(settings.limits.job_concurrency)
        self._resource_locks: dict[str, asyncio.Lock] = {}
        self._handlers: dict[str, JobHandler] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        self._event_counts: dict[str, int] = {}
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Prepare the log dir and fail any jobs left running by a prior process."""
        self._log_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        async with self._sessionmaker() as db:
            # Queued jobs from a prior process have no in-memory handler and can
            # never run, so they are interrupted too (not just running ones).
            await db.execute(
                update(Job)
                .where(
                    Job.status.in_(
                        [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED]
                    )
                )
                .values(
                    status=JobStatus.FAILED,
                    error_code="job.interrupted",
                    error_message="Interrupted by a process restart.",
                    finished_at=_now(),
                )
            )
            await db.commit()
        self._started = True

    async def stop(self) -> None:
        """Cancel in-flight job tasks and let them persist their final state."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- submission ----------------------------------------------------------

    async def submit(
        self,
        *,
        type: str,
        title: str,
        handler: JobHandler,
        params: dict[str, Any] | None = None,
        resource_key: str | None = None,
        request_id: str = "",
    ) -> str:
        """Persist a queued job and schedule it; returns the new job id."""
        job_id = uuid4().hex
        async with self._sessionmaker() as db:
            db.add(
                Job(
                    id=job_id,
                    type=type,
                    title=title,
                    status=JobStatus.QUEUED,
                    progress=None,
                    stage=None,
                    created_at=_now(),
                    request_id=request_id,
                    log_path=str(self._log_dir / f"{job_id}.log"),
                    params=json.dumps(params) if params is not None else None,
                    resource_key=resource_key,
                )
            )
            await db.commit()
        self._handlers[job_id] = handler
        self._event_counts[job_id] = 0
        self._publish(job_id, JobStatus.QUEUED, None, None)
        self._tasks[job_id] = asyncio.create_task(self._run(job_id, resource_key))
        return job_id

    # -- worker --------------------------------------------------------------

    async def _run(self, job_id: str, resource_key: str | None) -> None:
        handler = self._handlers.get(job_id)
        if handler is None:  # pragma: no cover - defensive
            return
        try:
            async with self._semaphore:
                if job_id in self._cancel_requested:
                    await self._finish(job_id, JobStatus.CANCELLED)
                    return
                if resource_key is not None:
                    lock = self._resource_locks.setdefault(resource_key, asyncio.Lock())
                    async with lock:
                        if job_id in self._cancel_requested:
                            await self._finish(job_id, JobStatus.CANCELLED)
                            return
                        await self._execute(job_id, handler)
                else:
                    await self._execute(job_id, handler)
        except asyncio.CancelledError:
            # Cancelled while parked on the semaphore or resource lock, before
            # _execute ran (which would otherwise persist the terminal state).
            await self._finish(job_id, JobStatus.CANCELLED)
        finally:
            self._tasks.pop(job_id, None)
            self._cancel_requested.discard(job_id)
            self._handlers.pop(job_id, None)
            self._event_counts.pop(job_id, None)

    async def _execute(self, job_id: str, handler: JobHandler) -> None:
        await self._mark_running(job_id)
        ctx = JobContext(self, job_id)
        try:
            await handler(ctx)
            await self._finish(job_id, JobStatus.SUCCEEDED)
        except (asyncio.CancelledError, JobCancelled):
            await self._finish(job_id, JobStatus.CANCELLED)
        except Exception as exc:
            log.exception("job.failed", job_id=job_id)
            await self._finish(
                job_id, JobStatus.FAILED, error_code="job.error", error_message=str(exc)
            )

    # -- state transitions ---------------------------------------------------

    async def _mark_running(self, job_id: str) -> None:
        async with self._sessionmaker() as db:
            await db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status=JobStatus.RUNNING, started_at=_now(), progress=0)
            )
            await db.commit()
        self._publish(job_id, JobStatus.RUNNING, 0, None)

    async def _finish(
        self,
        job_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._sessionmaker() as db:
            # Guard against clobbering an already-terminal state (a cancel racing
            # a completing job, or a double-finish): the first terminal write wins.
            result = await db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status.notin_(_TERMINAL_STATUSES))
                .values(
                    status=status,
                    finished_at=_now(),
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            await db.commit()
        if isinstance(result, CursorResult) and result.rowcount:
            self._publish(job_id, status, None, None, error_code=error_code)

    async def _job_progress(self, job_id: str, value: int | None, *, stage: str | None) -> None:
        async with self._sessionmaker() as db:
            values: dict[str, Any] = {"progress": value}
            if stage is not None:
                values["stage"] = stage
            await db.execute(update(Job).where(Job.id == job_id).values(**values))
            await db.commit()
        self._publish(job_id, JobStatus.RUNNING, value, stage)

    async def _job_log(self, job_id: str, message: str, *, level: str) -> None:
        ts = _now()
        line = f"{ts.isoformat()} {level.upper()} {message}\n"
        path = self._log_dir / f"{job_id}.log"
        await asyncio.to_thread(self._append_line, path, line)
        count = self._event_counts.get(job_id, 0)
        if level != "debug" and count < _MAX_JOB_EVENTS:
            self._event_counts[job_id] = count + 1
            async with self._sessionmaker() as db:
                db.add(JobEvent(job_id=job_id, ts=ts, level=level, message=message))
                await db.commit()
        self._events.publish(
            "job-log", {"job_id": job_id, "ts": ts.isoformat(), "level": level, "message": message}
        )

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _publish(
        self,
        job_id: str,
        status: str,
        progress: int | None,
        stage: str | None,
        *,
        error_code: str | None = None,
    ) -> None:
        self._events.publish(
            "job",
            {
                "job_id": job_id,
                "status": status,
                "progress": progress,
                "stage": stage,
                "error_code": error_code,
            },
        )

    # -- cancellation --------------------------------------------------------

    def _is_cancel_requested(self, job_id: str) -> bool:
        return job_id in self._cancel_requested

    async def cancel(self, job_id: str) -> None:
        """Request cancellation: queued jobs cancel now, running jobs are signalled."""
        async with self._sessionmaker() as db:
            job = await db.get(Job, job_id)
            if job is None:
                from mypanel.errors import NotFoundError

                raise NotFoundError("Unknown job.")
            if job.status in _TERMINAL_STATUSES:
                return
            self._cancel_requested.add(job_id)
            if job.status == JobStatus.RUNNING:
                # Only demote a still-running job; a conditional write avoids
                # clobbering a terminal state set concurrently by _finish.
                await db.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
                    .values(status=JobStatus.CANCEL_REQUESTED)
                )
                await db.commit()
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
        elif job.status == JobStatus.QUEUED:
            # No live task (e.g. it was reaped) but still queued in the DB: make
            # sure it reaches a terminal state rather than lingering forever.
            await self._finish(job_id, JobStatus.CANCELLED)

    # -- queries -------------------------------------------------------------

    async def get(self, job_id: str) -> dict[str, Any] | None:
        async with self._sessionmaker() as db:
            job = await db.get(Job, job_id)
            return _job_to_dict(job) if job is not None else None

    async def list(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        async with self._sessionmaker() as db:
            stmt = select(Job).order_by(Job.created_at.desc())
            if status is not None:
                stmt = stmt.where(Job.status == status)
            rows = (await db.scalars(stmt.limit(limit).offset(offset))).all()
            total = len(
                (
                    await db.scalars(
                        select(Job.id).where(Job.status == status)
                        if status is not None
                        else select(Job.id)
                    )
                ).all()
            )
        return {"jobs": [_job_to_dict(j) for j in rows], "total": total}

    async def stream_log(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Replay the log file, then follow live ``job-log`` events for this job."""
        path = self._log_dir / f"{job_id}.log"
        if path.is_file():
            text = await asyncio.to_thread(path.read_text, "utf-8")
            for entry in text.splitlines():
                yield {"replay": True, "line": entry}
        async with self._events.subscribe() as events:
            async for topic, data in events:
                if topic == "job-log" and data.get("job_id") == job_id:
                    yield data
                if (
                    topic == "job"
                    and data.get("job_id") == job_id
                    and data.get("status")
                    in (
                        JobStatus.SUCCEEDED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                    )
                ):
                    yield {"done": True, "status": data.get("status")}
                    return
