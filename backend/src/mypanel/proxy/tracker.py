"""Background tracker for Clash connections and traffic history (§ proxy module).

Polls the Clash external controller so the panel can show (a) live active
connections plus the last N closed ones — which the controller does not retain —
and (b) per-minute up/down traffic history for a selectable window (default 24h),
which the controller only exposes as a real-time stream. The poller is resilient:
if the controller is unreachable it backs off and keeps retrying, never crashing.

Two asyncio tasks run for the app's lifetime (started by the lifespan through
``app.state.proxy_tracker``): a ``/connections`` poll every ~2s that diffs
snapshots to detect closed connections, and a ``/traffic`` stream reader that
aggregates the 1 Hz rate samples into per-minute averages kept in a 7-day
in-memory ring. The ``ingest_*`` methods are pure state updates so tests can
drive them with canned payloads without a live controller.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from mypanel.config import Settings
from mypanel.logging import get_logger
from mypanel.proxy.clash import ClashClient

log = get_logger(__name__)

_POLL_INTERVAL_S = 2.0
_BACKOFF_START_S = 2.0
_BACKOFF_MAX_S = 30.0
#: Closed-connection history kept for the connections view.
CLOSED_MAX = 100
#: Per-minute traffic samples kept in memory (7 days).
TRAFFIC_MAX_MINUTES = 7 * 24 * 60


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _shape_connection(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce one controller connection object to the panel's stable shape."""
    conn_id = _as_str(raw.get("id"))
    if not conn_id:
        return None
    meta_raw = raw.get("metadata")
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    dest_ip = _as_str(meta.get("destinationIP"))
    dest_port = _as_str(meta.get("destinationPort"))
    destination = f"{dest_ip}:{dest_port}" if dest_ip else ""
    chains_raw = raw.get("chains")
    chains = [c for c in chains_raw if isinstance(c, str)] if isinstance(chains_raw, list) else []
    return {
        "id": conn_id,
        "host": _as_str(meta.get("host")) or destination,
        "network": _as_str(meta.get("network")),
        "type": _as_str(meta.get("type")),
        "source": f"{_as_str(meta.get('sourceIP'))}:{_as_str(meta.get('sourcePort'))}",
        "destination": destination,
        "rule": _as_str(raw.get("rule")),
        "rule_payload": _as_str(raw.get("rulePayload")),
        "chains": chains,
        "upload": _as_int(raw.get("upload")),
        "download": _as_int(raw.get("download")),
        "start": _as_str(raw.get("start")),
    }


class ProxyTracker:
    """Owns the Clash polling loop and in-memory connection/traffic history."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tasks: list[asyncio.Task[None]] = []
        self._conn_client: ClashClient | None = None
        self._traffic_client: ClashClient | None = None
        # Set by stop(): the loops watch it so shutdown never depends on a
        # CancelledError actually reaching the loop body (httpx/anyio can
        # swallow a cancellation that lands mid-request and surface it as a
        # plain transport error instead).
        self._closing = asyncio.Event()
        # -- connections state --
        self._available = False
        self._active: list[dict[str, Any]] = []
        self._last_by_id: dict[str, dict[str, Any]] = {}
        self._closed: deque[dict[str, Any]] = deque(maxlen=CLOSED_MAX)
        self._download_total = 0
        self._upload_total = 0
        self._updated_at: str | None = None
        self._last_poll_at: datetime | None = None
        # -- traffic state --
        self._traffic: deque[dict[str, Any]] = deque(maxlen=TRAFFIC_MAX_MINUTES)
        self._latest_rate: dict[str, Any] = {"up": 0, "down": 0}
        self._bucket_minute: datetime | None = None
        self._bucket_up = 0
        self._bucket_down = 0
        self._bucket_count = 0

    def _make_client(self) -> ClashClient:
        proxy = self._settings.proxy
        return ClashClient(proxy.clash_api, proxy.clash_secret)

    async def start(self) -> None:
        """Begin the background poll loops (safe to call when Clash is down)."""
        self._closing.clear()
        self._conn_client = self._make_client()
        self._traffic_client = self._make_client()
        self._tasks = [
            asyncio.create_task(self._connections_loop()),
            asyncio.create_task(self._traffic_loop()),
        ]

    async def stop(self) -> None:
        """Stop the background poll loops."""
        self._closing.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            # The closing event makes the loops exit on their own even when the
            # cancellation was swallowed inside a transport call; the timeout is
            # a last-resort bound and closing the clients below aborts whatever
            # request a task might still be stuck in.
            await asyncio.wait(self._tasks, timeout=5.0)
        self._tasks = []
        for client in (self._conn_client, self._traffic_client):
            if client is not None:
                await client.aclose()
        self._conn_client = None
        self._traffic_client = None

    async def _pause(self, seconds: float) -> None:
        """Sleep that wakes up immediately when stop() is called."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._closing.wait(), timeout=seconds)

    # -- snapshot reads (router-facing) --------------------------------------

    def connections(self) -> dict[str, Any]:
        """Current active connections plus the last closed ones (newest first)."""
        return {
            "available": self._available,
            "active": list(self._active),
            "closed": list(self._closed),
            "download_total": self._download_total,
            "upload_total": self._upload_total,
            "updated_at": self._updated_at,
        }

    def traffic_history(self, minutes: int) -> list[dict[str, Any]]:
        """Per-minute up/down samples for the window: [{ts, up, down}, ...]."""
        cutoff = (datetime.now(tz=UTC) - timedelta(minutes=minutes)).replace(microsecond=0)
        cutoff_iso = cutoff.isoformat()
        return [s for s in self._traffic if s["ts"] >= cutoff_iso]

    def traffic_latest(self) -> dict[str, Any]:
        """Latest instantaneous rate {"up": bytes_per_s, "down": bytes_per_s}."""
        return dict(self._latest_rate)

    # -- state updates (poll loops and tests) --------------------------------

    def ingest_connections(self, payload: dict[str, Any], now: datetime) -> None:
        """Fold one ``GET /connections`` snapshot into the tracked state.

        Connections present in the previous snapshot but missing from this one
        are recorded as closed (with their last-seen counters and ``closed_at``).
        Per-connection rates are derived from the byte-counter delta between
        consecutive snapshots.
        """
        raw_list = payload.get("connections")
        items = raw_list if isinstance(raw_list, list) else []
        now_iso = now.isoformat()
        dt = (now - self._last_poll_at).total_seconds() if self._last_poll_at else 0.0
        seen: dict[str, dict[str, Any]] = {}
        active: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            entry = _shape_connection(raw)
            if entry is None:
                continue
            prev = self._last_by_id.get(entry["id"])
            if prev is not None and dt > 0:
                entry["upload_rate"] = max(0.0, (entry["upload"] - prev["upload"]) / dt)
                entry["download_rate"] = max(0.0, (entry["download"] - prev["download"]) / dt)
            else:
                entry["upload_rate"] = 0.0
                entry["download_rate"] = 0.0
            seen[entry["id"]] = entry
            active.append(entry)
        for conn_id, prev in self._last_by_id.items():
            if conn_id not in seen:
                skip = ("upload_rate", "download_rate")
                closed = {k: v for k, v in prev.items() if k not in skip}
                closed["closed_at"] = now_iso
                self._closed.appendleft(closed)
        self._last_by_id = seen
        self._active = active
        self._download_total = _as_int(payload.get("downloadTotal"))
        self._upload_total = _as_int(payload.get("uploadTotal"))
        self._available = True
        self._updated_at = now_iso
        self._last_poll_at = now

    def mark_unavailable(self) -> None:
        """Controller unreachable: clear live state, keep closed/traffic history.

        Connections active at the time of the outage are dropped (not moved to
        ``closed``) because their fate is unknown.
        """
        self._available = False
        self._active = []
        self._last_by_id = {}
        self._last_poll_at = None
        self._latest_rate = {"up": 0, "down": 0}

    def ingest_traffic(self, up: int, down: int, now: datetime) -> None:
        """Fold one 1 Hz traffic sample (bytes/s) into the per-minute history."""
        self._latest_rate = {"up": up, "down": down}
        minute = now.replace(second=0, microsecond=0)
        if self._bucket_minute is not None and minute != self._bucket_minute:
            self._flush_traffic_bucket()
        if self._bucket_minute is None or minute != self._bucket_minute:
            self._bucket_minute = minute
            self._bucket_up = 0
            self._bucket_down = 0
            self._bucket_count = 0
        self._bucket_up += up
        self._bucket_down += down
        self._bucket_count += 1

    def _flush_traffic_bucket(self) -> None:
        if self._bucket_minute is None or self._bucket_count == 0:
            return
        self._traffic.append(
            {
                "ts": self._bucket_minute.isoformat(),
                "up": round(self._bucket_up / self._bucket_count),
                "down": round(self._bucket_down / self._bucket_count),
            }
        )
        self._bucket_minute = None
        self._bucket_count = 0

    # -- poll loops -----------------------------------------------------------

    async def _connections_loop(self) -> None:
        backoff = _BACKOFF_START_S
        while not self._closing.is_set():
            client = self._conn_client
            if client is None:  # pragma: no cover - stop() raced the loop
                return
            try:
                payload = await client.connections()
            except httpx.HTTPError:
                if self._available:
                    log.info("proxy.tracker_controller_lost")
                self.mark_unavailable()
                await self._pause(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
                continue
            except Exception:  # keep the loop alive on unexpected errors
                log.exception("proxy.tracker_connections_failed")
                self.mark_unavailable()
                await self._pause(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
                continue
            self.ingest_connections(payload, datetime.now(tz=UTC))
            backoff = _BACKOFF_START_S
            await self._pause(_POLL_INTERVAL_S)

    async def _traffic_loop(self) -> None:
        backoff = _BACKOFF_START_S
        while not self._closing.is_set():
            client = self._traffic_client
            if client is None:  # pragma: no cover - stop() raced the loop
                return
            try:
                async with client.stream_traffic() as lines:
                    backoff = _BACKOFF_START_S
                    async for raw in lines:
                        if self._closing.is_set():
                            break
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, dict):
                            self.ingest_traffic(
                                _as_int(data.get("up")),
                                _as_int(data.get("down")),
                                datetime.now(tz=UTC),
                            )
            except httpx.HTTPError:
                pass
            except Exception:  # keep the loop alive on unexpected errors
                log.exception("proxy.tracker_traffic_failed")
            # Stream ended or failed: drop the live rate and reconnect with backoff.
            self._latest_rate = {"up": 0, "down": 0}
            await self._pause(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
