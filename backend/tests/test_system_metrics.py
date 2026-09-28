"""GET /system/metrics: history windowing, ``max_points`` downsampling, auth.

The background sampler started by the lifespan may persist one real sample of
its own right after startup (ts close to "now"). Seeded rows are backdated at
least one minute, so they always sort strictly before that real sample; the
assertions below only rely on the seeded rows forming a prefix of the response.
"""

from __future__ import annotations

import json
from datetime import UTC, timedelta
from typing import Any

import httpx
import pytest
from conftest import API, OwnerContext, db_execute, naive_utcnow
from fastapi import FastAPI
from sqlalchemy import insert

from ownpanel.db.models import MetricSample
from ownpanel.system.router import _downsample

METRICS = f"{API}/system/metrics"


def _fake_samples(count: int) -> list[dict[str, Any]]:
    """``count`` minimal samples, oldest first, one per minute ending 1min ago.

    ``cpu_percent`` encodes the position (0.0 = oldest) so tests can identify
    which rows survived downsampling.
    """
    now = naive_utcnow()
    out: list[dict[str, Any]] = []
    for i in range(count):
        ts = now - timedelta(minutes=count - i)
        out.append(
            {
                "ts": ts.replace(tzinfo=UTC).isoformat(),
                "cpu_percent": float(i),
                "_row_ts": ts,
            }
        )
    return out


async def _seed_history(app: FastAPI, samples: list[dict[str, Any]]) -> None:
    """Insert one ``metric_samples`` row per fake sample (ts naive UTC)."""
    rows = [
        {
            "ts": s["_row_ts"],
            "data": json.dumps({"ts": s["ts"], "cpu_percent": s["cpu_percent"]}),
        }
        for s in samples
    ]
    await db_execute(app, insert(MetricSample).values(rows))


# -- _downsample (pure function) ---------------------------------------------


def test_downsample_returns_input_when_it_fits() -> None:
    samples = [{"i": i} for i in range(100)]
    assert _downsample(samples, 100) is samples
    assert _downsample(samples, 2000) is samples
    assert _downsample([], 100) == []


def test_downsample_thins_evenly_and_keeps_endpoints() -> None:
    samples = [{"i": i} for i in range(1001)]
    thinned = _downsample(samples, 100)
    assert len(thinned) <= 100
    assert thinned[0] is samples[0]
    assert thinned[-1] is samples[-1]
    # Still a subsequence of the input: strictly increasing positions.
    positions = [s["i"] for s in thinned]
    assert positions == sorted(set(positions))


def test_downsample_exact_multiple_replaces_tail() -> None:
    # total == stride * max_points: the strided pick misses the last sample and
    # the list is already full, so the tail must be replaced, not appended.
    samples = [{"i": i} for i in range(200)]
    thinned = _downsample(samples, 100)
    assert len(thinned) <= 100
    assert thinned[-1] is samples[-1]


@pytest.mark.parametrize("total", [101, 199, 200, 201, 1000, 10080])
def test_downsample_never_exceeds_max_points(total: int) -> None:
    samples = [{"i": i} for i in range(total)]
    thinned = _downsample(samples, 100)
    assert len(thinned) <= 100
    assert thinned[0] is samples[0]
    assert thinned[-1] is samples[-1]


# -- GET /system/metrics ------------------------------------------------------


async def test_metrics_history_window(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    seeded = _fake_samples(150)
    await _seed_history(app, seeded)

    resp = await client.get(METRICS, params={"minutes": 1440})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "history"
    samples = body["samples"]
    # All seeded rows are present (below the default max_points=2000) and form
    # an ordered prefix; a real sampler row may follow at the tail.
    assert len(samples) >= 150
    assert [s["cpu_percent"] for s in samples[:150]] == [float(i) for i in range(150)]
    assert [s["ts"] for s in samples] == sorted(s["ts"] for s in samples)


async def test_metrics_history_downsampled(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    seeded = _fake_samples(300)
    await _seed_history(app, seeded)

    resp = await client.get(METRICS, params={"minutes": 1440, "max_points": 100})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "history"
    samples = body["samples"]
    assert len(samples) <= 100
    # Oldest seeded row survives (stride starts at the first sample) and the
    # newest returned sample is not older than the newest seeded row.
    assert samples[0]["cpu_percent"] == 0.0
    assert samples[-1]["ts"] >= seeded[-1]["ts"]


async def test_metrics_param_validation(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    for params in (
        {"minutes": 0},
        {"minutes": 10081},
        {"max_points": 99},
        {"max_points": 10081},
    ):
        resp = await client.get(METRICS, params=params)
        assert resp.status_code == 422, f"{params}: {resp.status_code} {resp.text}"


async def test_metrics_requires_active_session(client: httpx.AsyncClient) -> None:
    resp = await client.get(METRICS)
    assert resp.status_code == 401


# —— GPU CSV parsing (pure function) ——


def test_parse_gpu_csv_parses_and_skips_bad_lines() -> None:
    from ownpanel.metrics.sampler import _parse_gpu_csv

    text = (
        "0, NVIDIA GeForce RTX 2080 Ti, 4, 807, 11264, 46\n"
        "1, Some GPU, 88, 100, 8192, 60\n"
        "garbage line\n"
        "2, Bad GPU, not-a-number, 0, 0, 0\n"
    )
    gpus = _parse_gpu_csv(text)
    assert [g["index"] for g in gpus] == [0, 1]
    assert gpus[0]["name"] == "NVIDIA GeForce RTX 2080 Ti"
    assert gpus[0]["util_percent"] == 4.0
    assert gpus[0]["mem_total_mb"] == 11264.0
    assert gpus[0]["temp_c"] == 46.0


def test_parse_gpu_csv_empty() -> None:
    from ownpanel.metrics.sampler import _parse_gpu_csv

    assert _parse_gpu_csv("") == []
