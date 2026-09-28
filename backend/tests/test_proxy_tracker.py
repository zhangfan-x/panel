"""ProxyTracker unit tests: pure ingest logic, no live controller or app.

The tracker's ``ingest_connections`` / ``ingest_traffic`` methods are plain
state updates driven here with canned payloads and fixed timestamps; the
asyncio poll loops themselves are exercised indirectly by the router tests
(which run the app lifespan with the controller down).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from conftest import SettingsFactory

from mypanel.proxy.tracker import CLOSED_MAX, ProxyTracker


def _conn(conn_id: str, *, upload: int = 0, download: int = 0) -> dict[str, Any]:
    return {
        "id": conn_id,
        "metadata": {
            "network": "tcp",
            "type": "HTTP",
            "host": f"{conn_id}.example.com",
            "sourceIP": "192.168.1.2",
            "sourcePort": "50000",
            "destinationIP": "1.2.3.4",
            "destinationPort": "443",
        },
        "upload": upload,
        "download": download,
        "start": "2026-07-22T00:00:00Z",
        "chains": ["Proxy", "HK"],
        "rule": "DOMAIN-SUFFIX",
        "rulePayload": "example.com",
    }


def _payload(*conns: dict[str, Any], down_total: int = 0, up_total: int = 0) -> dict[str, Any]:
    return {"connections": list(conns), "downloadTotal": down_total, "uploadTotal": up_total}


def make_tracker(make_settings: SettingsFactory) -> ProxyTracker:
    return ProxyTracker(make_settings())


def test_first_snapshot_shapes_active_without_rates(make_settings: SettingsFactory) -> None:
    tracker = make_tracker(make_settings)
    now = datetime.now(tz=UTC)
    tracker.ingest_connections(_payload(_conn("a", download=100), down_total=100), now)

    snap = tracker.connections()
    assert snap["available"] is True
    assert snap["download_total"] == 100
    assert snap["closed"] == []
    (active,) = snap["active"]
    assert active["host"] == "a.example.com"
    assert active["destination"] == "1.2.3.4:443"
    assert active["download_rate"] == 0.0  # no previous sample to diff against


def test_rates_derive_from_counter_deltas(make_settings: SettingsFactory) -> None:
    tracker = make_tracker(make_settings)
    t0 = datetime.now(tz=UTC)
    tracker.ingest_connections(_payload(_conn("a", upload=100, download=1000)), t0)
    tracker.ingest_connections(
        _payload(_conn("a", upload=300, download=5000)), t0 + timedelta(seconds=2)
    )

    (active,) = tracker.connections()["active"]
    assert active["upload_rate"] == 100.0
    assert active["download_rate"] == 2000.0


def test_disappeared_connections_become_closed_newest_first(
    make_settings: SettingsFactory,
) -> None:
    tracker = make_tracker(make_settings)
    t0 = datetime.now(tz=UTC)
    tracker.ingest_connections(_payload(_conn("a"), _conn("b", download=42)), t0)
    tracker.ingest_connections(_payload(_conn("a")), t0 + timedelta(seconds=2))
    tracker.ingest_connections(_payload(), t0 + timedelta(seconds=4))

    snap = tracker.connections()
    assert snap["active"] == []
    assert [c["id"] for c in snap["closed"]] == ["a", "b"]  # newest first
    b = snap["closed"][1]
    assert b["download"] == 42
    assert b["closed_at"] == (t0 + timedelta(seconds=2)).isoformat()
    assert "download_rate" not in b


def test_closed_history_is_capped(make_settings: SettingsFactory) -> None:
    tracker = make_tracker(make_settings)
    t = datetime.now(tz=UTC)
    for i in range(CLOSED_MAX + 20):
        tracker.ingest_connections(_payload(_conn(f"c{i}")), t)
        t += timedelta(seconds=1)
        tracker.ingest_connections(_payload(), t)
        t += timedelta(seconds=1)
    closed = tracker.connections()["closed"]
    assert len(closed) == CLOSED_MAX
    assert closed[0]["id"] == f"c{CLOSED_MAX + 19}"  # newest kept, oldest dropped


def test_malformed_payload_is_tolerated(make_settings: SettingsFactory) -> None:
    tracker = make_tracker(make_settings)
    now = datetime.now(tz=UTC)
    tracker.ingest_connections(
        {
            "connections": [
                "not-a-dict",
                {"metadata": {}},  # missing id
                {"id": "ok", "metadata": None, "chains": "nope", "upload": "x"},
            ],
            "downloadTotal": "broken",
        },
        now,
    )
    snap = tracker.connections()
    (active,) = snap["active"]
    assert active["id"] == "ok"
    assert active["chains"] == []
    assert active["upload"] == 0
    assert snap["download_total"] == 0


def test_mark_unavailable_clears_live_state_keeps_closed(
    make_settings: SettingsFactory,
) -> None:
    tracker = make_tracker(make_settings)
    t0 = datetime.now(tz=UTC)
    tracker.ingest_connections(_payload(_conn("a"), _conn("b")), t0)
    tracker.ingest_connections(_payload(_conn("a")), t0 + timedelta(seconds=2))

    tracker.mark_unavailable()
    snap = tracker.connections()
    assert snap["available"] is False
    assert snap["active"] == []
    # Connections live at the moment of the outage are dropped, not "closed".
    assert [c["id"] for c in snap["closed"]] == ["b"]

    # Recovery starts a fresh diff base: no phantom closes, no bogus rates.
    tracker.ingest_connections(_payload(_conn("a", upload=10**9)), t0 + timedelta(seconds=60))
    snap = tracker.connections()
    assert snap["available"] is True
    assert [c["id"] for c in snap["closed"]] == ["b"]
    assert snap["active"][0]["upload_rate"] == 0.0


def test_traffic_minute_aggregation_and_window(make_settings: SettingsFactory) -> None:
    tracker = make_tracker(make_settings)
    base = datetime.now(tz=UTC).replace(second=0, microsecond=0)

    old = base - timedelta(hours=2)
    for i in range(3):
        tracker.ingest_traffic(600, 1200, old + timedelta(seconds=i))
    recent = base - timedelta(minutes=5)
    tracker.ingest_traffic(1000, 2000, recent)
    tracker.ingest_traffic(2000, 4000, recent + timedelta(seconds=30))
    # Crossing into the next minute flushes the completed bucket.
    tracker.ingest_traffic(7, 9, recent + timedelta(minutes=1))

    assert tracker.traffic_latest() == {"up": 7, "down": 9}

    hour = tracker.traffic_history(60)
    assert len(hour) == 1
    assert hour[0] == {"ts": recent.isoformat(), "up": 1500, "down": 3000}

    three_hours = tracker.traffic_history(180)
    assert [s["ts"] for s in three_hours] == [old.isoformat(), recent.isoformat()]
    assert three_hours[0] == {"ts": old.isoformat(), "up": 600, "down": 1200}
