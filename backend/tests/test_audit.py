"""Audit query API: GET /audit list/filters/pagination and GET /audit/{event_id}.

Covers docs/code-plan-m2-m5.md §4.8. Rows are produced through real endpoints:
the ``owner`` fixture's bootstrap writes one audit row, then one successful and
one denied login add two more — no rows are inserted by hand.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from conftest import API, OwnerContext

AUDIT = f"{API}/audit"
LOGIN = f"{API}/auth/login"

EVENT_KEYS = {
    "event_id",
    "ts",
    "request_id",
    "actor_user_id",
    "source_ip",
    "user_agent",
    "resource",
    "action",
    "target",
    "result",
    "error_code",
    "duration_ms",
}


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


async def seed_login_rows(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    """Write two audit rows on top of the bootstrap one: an ok and a denied login."""
    resp = await client.post(LOGIN, json={"username": owner.username, "password": owner.password})
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        LOGIN, json={"username": owner.username, "password": "definitely-wrong-9!"}
    )
    assert resp.status_code == 401


async def list_events(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    resp = await client.get(AUDIT, params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_audit_requires_auth(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get(AUDIT), 401, "auth.required")
    assert_problem(await client.get(f"{AUDIT}/{'0' * 32}"), 401, "auth.required")


async def test_list_returns_rows_written_by_auth_actions(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    await seed_login_rows(client, owner)
    body = await list_events(client)
    assert body["total"] == 3
    events = body["events"]
    assert len(events) == 3
    for event in events:
        assert set(event) == EVENT_KEYS

    # Newest first: denied login, ok login, then the bootstrap row.
    assert [e["action"] for e in events] == ["auth.login", "auth.login", "bootstrap.create_owner"]
    assert [e["result"] for e in events] == ["denied", "ok", "ok"]
    ts_values = [datetime.fromisoformat(e["ts"]) for e in events]
    assert all(ts.tzinfo is not None for ts in ts_values)
    assert ts_values == sorted(ts_values, reverse=True)

    denied, ok_login, bootstrap = events
    assert denied["error_code"] == "auth.invalid_credentials"
    assert denied["actor_user_id"] is None
    assert ok_login["actor_user_id"] == 1
    assert ok_login["error_code"] is None
    assert bootstrap["resource"] == "bootstrap"
    assert bootstrap["target"] == owner.username
    for event in events:
        assert event["source_ip"] == "127.0.0.1"
        assert event["request_id"].startswith("req_")
        assert event["user_agent"]
        assert event["duration_ms"] == 0


async def test_list_filters(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    await seed_login_rows(client, owner)

    body = await list_events(client, result="denied")
    assert body["total"] == 1
    assert body["events"][0]["action"] == "auth.login"
    assert body["events"][0]["error_code"] == "auth.invalid_credentials"

    body = await list_events(client, action="auth.login")
    assert body["total"] == 2
    assert {e["action"] for e in body["events"]} == {"auth.login"}

    body = await list_events(client, resource="bootstrap")
    assert body["total"] == 1
    assert body["events"][0]["action"] == "bootstrap.create_owner"

    body = await list_events(client, action="auth.login", result="ok")
    assert body["total"] == 1

    body = await list_events(client, action="no.such.action")
    assert body == {"events": [], "total": 0}


async def test_list_since_until(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    await seed_login_rows(client, owner)
    everything = await list_events(client)
    middle_ts = everything["events"][1]["ts"]  # The successful login.

    assert (await list_events(client, since="2000-01-01T00:00:00Z"))["total"] == 3
    assert (await list_events(client, since="2000-01-01T00:00:00"))["total"] == 3
    assert (await list_events(client, until="2000-01-01T00:00:00Z"))["total"] == 0
    assert (await list_events(client, since="2100-01-01T00:00:00Z"))["total"] == 0

    newer = await list_events(client, since=middle_ts)
    assert newer["total"] == 2
    assert [e["result"] for e in newer["events"]] == ["denied", "ok"]
    older = await list_events(client, until=middle_ts)
    assert older["total"] == 2
    assert [e["action"] for e in older["events"]] == ["auth.login", "bootstrap.create_owner"]


async def test_list_pagination(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    await seed_login_rows(client, owner)

    body = await list_events(client, limit=1)
    assert body["total"] == 3
    assert len(body["events"]) == 1
    assert body["events"][0]["result"] == "denied"

    body = await list_events(client, limit=1, offset=1)
    assert body["total"] == 3
    assert len(body["events"]) == 1
    assert body["events"][0]["action"] == "auth.login"
    assert body["events"][0]["result"] == "ok"

    body = await list_events(client, limit=1, offset=99)
    assert body == {"events": [], "total": 3}


async def test_list_rejects_bad_query_params(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    assert_problem(await client.get(AUDIT, params={"limit": 0}), 422, "request.validation")
    assert_problem(await client.get(AUDIT, params={"limit": 201}), 422, "request.validation")
    assert_problem(await client.get(AUDIT, params={"offset": -1}), 422, "request.validation")
    resp = await client.get(AUDIT, params={"since": "not-a-date"})
    assert_problem(resp, 422, "request.validation")


async def test_event_detail(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    await seed_login_rows(client, owner)
    listed = (await list_events(client))["events"][0]

    resp = await client.get(f"{AUDIT}/{listed['event_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == EVENT_KEYS
    assert body == listed


async def test_event_detail_unknown_is_404(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    assert_problem(await client.get(f"{AUDIT}/{'f' * 32}"), 404, "not_found")
