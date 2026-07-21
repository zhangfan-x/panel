"""In-process sliding-window rate limiter for login-style endpoints.

Designed for single-threaded asyncio use; no locking. State lives in process
memory only, so restarting the panel clears all counters (acceptable here).

The window is bookkept with ``time.monotonic()`` rather than the wall clock, so
an NTP step, an operator changing the system clock, or a VM snapshot restore
cannot expire recorded events early (which would refill an attacker's attempt
budget) or corrupt the Retry-After arithmetic.
"""

from __future__ import annotations

import math
from collections import deque
from time import monotonic

from mypanel.errors import RateLimitedError

_SWEEP_INTERVAL = 256


class SlidingWindowLimiter:
    """Per-key sliding-window event counter.

    Every `acquire` call records an event, including rejected ones, so hammering
    a limited key keeps extending the block. Expired events are pruned lazily on
    access, and fully-expired keys are swept periodically so the map does not
    accumulate garbage entries.
    """

    def __init__(self, max_events: int, window_s: float) -> None:
        self.max_events = max_events
        self.window = float(window_s)
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._acquire_count = 0

    def acquire(self, key: tuple[str, str]) -> None:
        """Record one event for `key`; raise if the window already holds max_events.

        Raises:
            RateLimitedError: With `retry_after_s` set to the (ceiled) seconds until
                a retry can succeed, assuming no further events for the key.
        """
        now = monotonic()
        cutoff = now - self.window
        self._acquire_count += 1
        if self._acquire_count % _SWEEP_INTERVAL == 0:
            self._sweep(cutoff)
        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
        else:
            while events and events[0] <= cutoff:
                events.popleft()
        events.append(now)
        if len(events) > self.max_events:
            # A retry succeeds once all but max_events - 1 older events have expired;
            # the last of those leaves the window at events[len - max_events] + window.
            blocking = events[len(events) - self.max_events]
            remaining = blocking + self.window - now
            raise RateLimitedError(retry_after_s=max(1, math.ceil(remaining)))

    def reset(self, key: tuple[str, str]) -> None:
        """Forget all events for `key` (e.g. after a successful login); unknown keys no-op."""
        self._events.pop(key, None)

    def _sweep(self, cutoff: float) -> None:
        """Drop keys whose newest event has already expired."""
        stale = [key for key, events in self._events.items() if not events or events[-1] <= cutoff]
        for key in stale:
            del self._events[key]
