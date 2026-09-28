"""In-process event bus and the multiplexed ``GET /events`` SSE stream.

One global SSE stream per client carries lightweight, always-on events
(``metrics`` samples and ``job`` status changes). Per-resource follow streams
(journald, container logs, job logs) are separate short-lived SSE endpoints —
see ``docs/code-plan-m2-m5.md`` §0.3. The bus is fully in-process: publishers
call :meth:`EventBus.publish` (non-blocking) and each subscriber drains its own
bounded queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from ownpanel.auth.deps import CurrentAuth, resolve_stream_auth
from ownpanel.logging import get_logger

log = get_logger(__name__)

_QUEUE_MAXSIZE = 512
_HEARTBEAT_S = 15.0

Event = tuple[str, dict[str, Any]]


class Subscription:
    """A single subscriber's queue; async-iterates ``(topic, data)`` events.

    ``__anext__`` is a plain coroutine (not an async generator), so wrapping it
    in ``asyncio.wait_for`` for heartbeats and letting the timeout cancel it is
    safe: only that one ``queue.get()`` await is cancelled, the queue and the
    subscription survive, and the next ``__anext__`` keeps delivering events.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    def _push(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest event to make room; live streams favour freshness.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(event)

    async def __aenter__(self) -> Subscription:
        self._bus._add(self)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._bus._remove(self)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()


class EventBus:
    """Fan-out of events to every active :class:`Subscription`."""

    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    def _add(self, sub: Subscription) -> None:
        self._subscribers.add(sub)

    def _remove(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)

    def publish(self, topic: str, data: dict[str, Any]) -> None:
        """Deliver ``(topic, data)`` to all subscribers; never blocks or raises."""
        event: Event = (topic, data)
        for sub in self._subscribers:
            sub._push(event)

    def subscribe(self) -> Subscription:
        """Create a subscription; use as ``async with bus.subscribe() as events:``."""
        return Subscription(self)

    def stats(self) -> dict[str, int]:
        return {"subscribers": len(self._subscribers)}


def _sse(topic: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {topic}\ndata: {payload}\n\n"


async def _event_source(request: Request) -> AsyncIterator[str]:
    bus: EventBus = request.app.state.events
    yield _sse("ready", {})
    async with bus.subscribe() as events:
        while True:
            if await request.is_disconnected():
                return
            try:
                topic, data = await asyncio.wait_for(events.__anext__(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                # Heartbeat comment keeps intermediaries from closing an idle stream.
                # The subscription survives the cancelled __anext__ (see Subscription).
                yield ": ping\n\n"
                continue
            yield _sse(topic, data)


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


events_router = APIRouter(tags=["events"])


@events_router.get("/events")
async def events_stream(
    request: Request,
    _auth: Annotated[CurrentAuth, Depends(resolve_stream_auth)],
) -> StreamingResponse:
    """Global multiplexed event stream (metrics + job status) for the SPA."""
    return StreamingResponse(
        _event_source(request),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
