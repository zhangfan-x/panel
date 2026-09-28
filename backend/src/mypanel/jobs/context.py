"""Job execution context handed to each job handler.

A handler is ``async def (ctx: JobContext) -> dict | None``. It reports progress
and log lines through the context; cancellation is cooperative via
:meth:`JobContext.check_cancel` (and the worker also cancels the task).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypanel.jobs.manager import JobManager


class JobCancelled(Exception):
    """Raised inside a handler when cancellation has been requested."""


class JobContext:
    """Handle passed to a running job handler for progress/log/cancel signalling."""

    def __init__(self, manager: JobManager, job_id: str) -> None:
        self._manager = manager
        self.job_id = job_id

    async def log(self, message: str, *, level: str = "info") -> None:
        """Append a log line (log file + milestone JobEvent + ``job-log`` event)."""
        await self._manager._job_log(self.job_id, message, level=level)

    async def progress(self, value: int | None, *, stage: str | None = None) -> None:
        """Update progress (0-100 or None) and optional stage label."""
        await self._manager._job_progress(self.job_id, value, stage=stage)

    def cancelled(self) -> bool:
        """True once cancellation has been requested for this job."""
        return self._manager._is_cancel_requested(self.job_id)

    async def check_cancel(self) -> None:
        """Raise :class:`JobCancelled` if cancellation was requested."""
        if self.cancelled():
            raise JobCancelled()
