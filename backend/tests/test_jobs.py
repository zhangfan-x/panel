"""Regression tests for the job manager and event bus (docs/code-plan-m2-m5 §2).

Covers the adversarial-review fixes: a queued job that is cancelled must reach a
terminal state instead of lingering (manager cancel path), and the SSE
subscription must survive a heartbeat-cancelled ``__anext__`` (events bus).
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import SettingsFactory
from fastapi import FastAPI

from ownpanel.config import Settings
from ownpanel.events import EventBus


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    """One worker slot so a second submitted job is provably queued."""
    return make_settings(limits={"job_concurrency": 1})


async def _wait_status(app: FastAPI, job_id: str, target: set[str], *, tries: int = 200) -> str:
    for _ in range(tries):
        job = await app.state.jobs.get(job_id)
        assert job is not None
        if job["status"] in target:
            return str(job["status"])
        await asyncio.sleep(0.02)
    pytest.fail(f"job {job_id} never reached {target}")


async def test_job_runs_to_success(app: FastAPI, client: object) -> None:
    jobs = app.state.jobs

    async def handler(ctx: object) -> dict[str, bool]:
        await ctx.log("working")  # type: ignore[attr-defined]
        return {"ok": True}

    job_id = await jobs.submit(type="test", title="ok job", handler=handler)
    assert await _wait_status(app, job_id, {"succeeded"}) == "succeeded"


async def test_cancel_queued_job_reaches_terminal_state(app: FastAPI, client: object) -> None:
    """A job cancelled while parked on the concurrency semaphore must not stick."""
    jobs = app.state.jobs
    gate = asyncio.Event()

    async def blocker(ctx: object) -> None:
        await gate.wait()

    async def queued_handler(ctx: object) -> None:
        return None

    running_id = await jobs.submit(type="test", title="blocker", handler=blocker)
    await _wait_status(app, running_id, {"running"})

    queued_id = await jobs.submit(type="test", title="queued", handler=queued_handler)
    parked = await jobs.get(queued_id)
    assert parked is not None
    assert parked["status"] == "queued"

    await jobs.cancel(queued_id)
    assert await _wait_status(app, queued_id, {"cancelled"}) == "cancelled"

    gate.set()
    await _wait_status(app, running_id, {"succeeded"})


async def test_finish_never_overwrites_a_terminal_state(app: FastAPI, client: object) -> None:
    """A late _finish (e.g. a lagging cancel) cannot clobber a completed job."""
    jobs = app.state.jobs

    async def handler(ctx: object) -> None:
        return None

    job_id = await jobs.submit(type="test", title="done", handler=handler)
    await _wait_status(app, job_id, {"succeeded"})

    # Simulate a lagging cancel arriving after completion: must be a no-op.
    await jobs._finish(job_id, "cancelled")  # type: ignore[attr-defined]
    job = await jobs.get(job_id)
    assert job is not None
    assert job["status"] == "succeeded"


async def test_subscription_survives_heartbeat_cancelled_anext() -> None:
    """wait_for cancelling __anext__ (heartbeat) must not kill the subscription."""
    bus = EventBus()
    async with bus.subscribe() as sub:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sub.__anext__(), timeout=0.05)
        bus.publish("metrics", {"cpu": 1})
        topic, data = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert topic == "metrics"
        assert data == {"cpu": 1}
    assert bus.stats()["subscribers"] == 0
