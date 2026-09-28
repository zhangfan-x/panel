"""Background metrics sampler: 5s in-memory ring buffer + 1min SQLite aggregate.

Runs as an asyncio task started by the app lifespan. Every ~5s it samples the
host via psutil, keeps the latest samples in a ring buffer for the dashboard's
live view, publishes them on the ``metrics`` event topic, and every ~60s writes
one aggregated row to ``metric_samples`` (pruned to ``metric_retention_days``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import psutil
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mypanel.config import Settings
from mypanel.db.models import MetricSample
from mypanel.events import EventBus
from mypanel.logging import get_logger

log = get_logger(__name__)

_SAMPLE_INTERVAL_S = 5.0
_AGGREGATE_EVERY = 12  # 12 * 5s = 60s
_RING_SIZE = 240  # ~20 min of 5s samples
_PRUNE_EVERY = 720  # 720 * 5s = 1h

# Pseudo / virtual filesystems that are not real storage; excluded from the
# dashboard disk list so snap loop mounts, tmpfs, overlays etc. don't drown out
# the actual block devices.
_SKIP_FSTYPES = frozenset(
    {
        "squashfs",
        "tmpfs",
        "devtmpfs",
        "overlay",
        "overlayfs",
        "aufs",
        "proc",
        "sysfs",
        "cgroup",
        "cgroup2",
        "pstore",
        "debugfs",
        "tracefs",
        "securityfs",
        "ramfs",
        "mqueue",
        "hugetlbfs",
        "fusectl",
        "configfs",
        "binfmt_misc",
        "autofs",
        "nsfs",
        "fuse.gvfsd-fuse",
        "fuse.portal",
    }
)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


#: nvidia-smi query for per-GPU utilisation/memory/temperature (no units, CSV).
_GPU_QUERY = (
    "nvidia-smi",
    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
    "--format=csv,noheader,nounits",
)
_GPU_TIMEOUT_S = 5.0


def _parse_gpu_csv(text: str) -> list[dict[str, Any]]:
    """Parse ``nvidia-smi`` CSV output into GPU snapshots; bad lines are skipped."""
    gpus: list[dict[str, Any]] = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "util_percent": float(parts[2]),
                    "mem_used_mb": float(parts[3]),
                    "mem_total_mb": float(parts[4]),
                    "temp_c": float(parts[5]),
                }
            )
        except ValueError:
            continue
    return gpus


class MetricsSampler:
    """Samples host metrics on a fixed cadence into a ring buffer and SQLite."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        events: EventBus,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._events = events
        self._ring: deque[dict[str, Any]] = deque(maxlen=_RING_SIZE)
        self._task: asyncio.Task[None] | None = None
        self._last_net: tuple[float, int, int] | None = None
        # None = not probed yet; False = nvidia-smi missing/failed, never retried.
        self._gpu_available: bool | None = None

    async def start(self) -> None:
        # Prime cpu_percent so the first real sample is not a bogus 0/100.
        psutil.cpu_percent(percpu=False)
        psutil.cpu_percent(percpu=True)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def recent(self, limit: int = 60) -> list[dict[str, Any]]:
        """Snapshot of the most recent in-memory samples (oldest → newest)."""
        items = list(self._ring)
        return items[-limit:] if limit > 0 else items

    def latest(self) -> dict[str, Any] | None:
        return self._ring[-1] if self._ring else None

    async def history(self, db: AsyncSession, *, minutes: int) -> list[dict[str, Any]]:
        """Aggregated samples from SQLite for the requested window."""
        from sqlalchemy import select

        since = datetime.now(tz=UTC) - timedelta(minutes=minutes)
        rows = (
            await db.scalars(
                select(MetricSample).where(MetricSample.ts >= since).order_by(MetricSample.ts)
            )
        ).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.append(json.loads(row.data))
            except json.JSONDecodeError:  # pragma: no cover - corrupt row
                continue
        return out

    # -- internals -----------------------------------------------------------

    async def _loop(self) -> None:
        tick = 0
        while True:
            try:
                sample = await asyncio.to_thread(self._sample)
                self._ring.append(sample)
                self._events.publish("metrics", sample)
                if tick % _AGGREGATE_EVERY == 0:
                    await self._persist(sample)
                if tick % _PRUNE_EVERY == 0 and tick > 0:
                    await self._prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("metrics.sample_failed")
            tick += 1
            await asyncio.sleep(_SAMPLE_INTERVAL_S)

    def _sample(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        try:
            load = psutil.getloadavg()
        except (OSError, AttributeError):  # pragma: no cover - platform dependent
            load = (0.0, 0.0, 0.0)
        disks: list[dict[str, Any]] = []
        for part in psutil.disk_partitions(all=False):
            # Skip pseudo filesystems and snap/loop mounts — keep real storage.
            if part.fstype.lower() in _SKIP_FSTYPES:
                continue
            if part.device.startswith("/dev/loop"):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            disks.append(
                {
                    "mount": part.mountpoint,
                    "device": part.device,
                    "fstype": part.fstype,
                    "total": usage.total,
                    "used": usage.used,
                    "percent": usage.percent,
                }
            )
        net = self._net_rates()
        return {
            "ts": _now_iso(),
            "cpu_percent": psutil.cpu_percent(percpu=False),
            "per_cpu": psutil.cpu_percent(percpu=True),
            "load": list(load),
            "mem": {
                "total": vm.total,
                "used": vm.used,
                "available": vm.available,
                "percent": vm.percent,
            },
            "swap": {"total": sm.total, "used": sm.used, "percent": sm.percent},
            "disks": disks,
            "net": net,
            "gpus": self._sample_gpus(),
        }

    def _sample_gpus(self) -> list[dict[str, Any]]:
        """Per-GPU snapshots via nvidia-smi (runs inside the to_thread sample).

        A missing binary or non-zero exit disables GPU sampling for the process
        lifetime; a transient timeout only skips the current tick.
        """
        if self._gpu_available is False:
            return []
        try:
            proc = subprocess.run(
                _GPU_QUERY, capture_output=True, text=True, timeout=_GPU_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return []
        except OSError:
            self._gpu_available = False
            return []
        if proc.returncode != 0:
            self._gpu_available = False
            return []
        self._gpu_available = True
        return _parse_gpu_csv(proc.stdout)

    def _net_rates(self) -> dict[str, Any]:
        counters = psutil.net_io_counters()
        now = time.monotonic()
        rx, tx = counters.bytes_recv, counters.bytes_sent
        rate_rx = rate_tx = 0.0
        if self._last_net is not None:
            last_t, last_rx, last_tx = self._last_net
            dt = now - last_t
            if dt > 0:
                rate_rx = max(0.0, (rx - last_rx) / dt)
                rate_tx = max(0.0, (tx - last_tx) / dt)
        self._last_net = (now, rx, tx)
        return {
            "rx_bytes_per_s": rate_rx,
            "tx_bytes_per_s": rate_tx,
            "rx_total": rx,
            "tx_total": tx,
        }

    async def _persist(self, sample: dict[str, Any]) -> None:
        async with self._sessionmaker() as db:
            db.add(MetricSample(ts=datetime.now(tz=UTC), data=json.dumps(sample)))
            await db.commit()

    async def _prune(self) -> None:
        cutoff = datetime.now(tz=UTC) - timedelta(days=self._settings.limits.metric_retention_days)
        async with self._sessionmaker() as db:
            await db.execute(delete(MetricSample).where(MetricSample.ts < cutoff))
            await db.commit()
