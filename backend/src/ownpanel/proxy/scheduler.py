"""Background refresh loop for subscription profiles.

Profiles carry an ``interval_min``; this loop wakes up once a minute, downloads
whatever is due, and — when the active profile was among them — re-runs the
synthesis pipeline so the new nodes actually take effect. A failure is recorded
on the profile (``last_error``, surfaced in the UI) and never escapes the loop.

Shutdown is driven by a ``closing`` event rather than task cancellation alone:
httpx/anyio can swallow a ``CancelledError`` that lands mid-request and surface
it as a plain transport error, so the loop must be able to notice on its own
that it should stop. Same shape as :class:`ownpanel.proxy.tracker.ProxyTracker`.
"""

from __future__ import annotations

import asyncio
import contextlib

from ownpanel.config import Settings
from ownpanel.logging import get_logger
from ownpanel.proxy import profiles as profile_store

log = get_logger(__name__)


class SubscriptionScheduler:
    """Periodically refreshes subscription profiles that are due."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._closing = asyncio.Event()

    async def start(self) -> None:
        """Begin the refresh loop (safe when no profiles exist yet)."""
        self._closing.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the refresh loop."""
        self._closing.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        # The closing event lets the loop exit on its own even when the
        # cancellation was swallowed inside a transport call; the timeout is a
        # last-resort bound.
        await asyncio.wait({task}, timeout=5.0)

    async def _pause(self, seconds: float) -> None:
        """Sleep that wakes up immediately when stop() is called."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._closing.wait(), timeout=seconds)

    async def _loop(self) -> None:
        interval = max(5.0, self._settings.proxy.subscription_check_interval_s)
        while not self._closing.is_set():
            await self._pause(interval)
            if self._closing.is_set():
                return
            try:
                await self.tick()
            except Exception:  # pragma: no cover - defensive; the loop must survive
                log.exception("proxy.subscription_tick_failed")

    async def tick(self) -> int:
        """Refresh every due profile once; returns how many were downloaded.

        Exposed separately from the loop so tests (and a future manual trigger)
        can drive one pass deterministically.
        """
        settings = self._settings
        profiles = profile_store.load_profiles(settings)
        due = profile_store.due_profiles(profiles, profile_store.utcnow())
        if not due:
            return 0

        refreshed = 0
        reapply = False
        for profile in due:
            if self._closing.is_set():
                break
            try:
                text, info = await profile_store.fetch_subscription(settings, profile)
            except Exception as exc:
                profile_store.mark_failed(profile, str(exc))
                log.warning(
                    "proxy.subscription_refresh_failed", profile=profile["id"], error=str(exc)
                )
                continue
            profile_store.store_raw(settings, profile["id"], text)
            profile_store.mark_updated(profile, info)
            refreshed += 1
            reapply = reapply or profile["active"]

        if reapply:
            try:
                await profile_store.apply_active(settings, profiles)
            except Exception as exc:
                active = profile_store.active_profile(profiles)
                if active is not None:
                    profile_store.mark_failed(active, f"应用配置失败: {exc}")
                log.warning("proxy.subscription_apply_failed", error=str(exc))

        profile_store.save_profiles(settings, profiles)
        return refreshed
