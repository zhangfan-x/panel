"""Proxy (Clash/Mihomo) module tests: pure parsers, ClashClient wiring, router.

Covers docs/code-plan-m2-m5.md proxy feature: :func:`parse_groups` /
:func:`parse_status` shaping, :class:`ClashClient` URL building + Bearer header
(via ``httpx.MockTransport``, no live controller), and the ``/proxy`` router
with the controller monkeypatched (status/groups/select/mode/config-file, CSRF
and reauth gates, and the 502 mapping for an unreachable controller).
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest
from conftest import (
    API,
    OwnerContext,
    csrf_headers,
    db_execute,
    db_scalar,
    naive_utcnow,
)
from sqlalchemy import select, update

from mypanel.db.models import Session
from mypanel.proxy import router as proxy_router
from mypanel.proxy.clash import DELAY_TEST_URL, ClashClient, parse_groups, parse_status

PROXY = f"{API}/proxy"

# A synthetic GET /proxies payload: three group types plus leaf nodes.
# "Proxy" is a group nested inside GLOBAL with no history of its own — mihomo
# never records history on group proxies, so its delay must be inherited from
# whatever "now" resolves to.
SAMPLE_PROXIES: dict[str, Any] = {
    "proxies": {
        "GLOBAL": {"type": "Selector", "now": "Auto", "all": ["Auto", "Proxy", "HK", "DIRECT"]},
        "Auto": {"type": "URLTest", "now": "HK", "all": ["HK", "JP"], "history": [{"delay": 88}]},
        "Proxy": {"type": "Selector", "now": "HK", "all": ["HK", "JP"], "history": []},
        "HK": {"type": "Shadowsocks", "history": [{"time": "t", "delay": 120}]},
        "JP": {"type": "Vmess", "history": []},
        "DIRECT": {"type": "Direct"},
        "REJECT": {"type": "Reject"},
    }
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


# —— Fakes standing in for a live controller in router tests ——


class FakeClash:
    """Canned ClashClient: never touches the network."""

    #: Set to None to emulate a core without ``GET /group/{name}/delay``.
    group_delay_result: ClassVar[dict[str, int] | None] = {"HK": 120, "JP": 300}
    #: Records the (group, timeout, url) of the last group probe.
    last_group_probe: ClassVar[tuple[str, int, str] | None] = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def aclose(self) -> None:
        return None

    async def version(self) -> dict[str, Any]:
        return {"version": "mihomo-1.18.0", "meta": True}

    async def configs(self) -> dict[str, Any]:
        return {
            "mode": "rule",
            "mixed-port": 7890,
            "port": 0,
            "socks-port": 0,
            "allow-lan": False,
            "log-level": "info",
        }

    async def proxies(self) -> dict[str, Any]:
        return SAMPLE_PROXIES

    async def proxy(self, name: str) -> dict[str, Any]:
        info = SAMPLE_PROXIES["proxies"].get(name)
        if not isinstance(info, dict):
            raise httpx.HTTPStatusError(
                "not found", request=httpx.Request("GET", "http://x"), response=httpx.Response(404)
            )
        return {**info, "testUrl": "http://cp.cloudflare.com/generate_204"}

    async def group_delay(
        self, group: str, *, timeout_ms: int = 5000, url: str = ""
    ) -> dict[str, int] | None:
        type(self).last_group_probe = (group, timeout_ms, url)
        return type(self).group_delay_result

    async def delay(self, name: str, *, timeout_ms: int = 5000, url: str = "") -> dict[str, Any]:
        return {"delay": {"HK": 120, "JP": 300}.get(name, 0)}

    async def select(self, group: str, name: str) -> None:
        return None

    async def patch_configs(self, payload: dict[str, Any]) -> None:
        return None

    async def reload_configs(self, *, path: str) -> None:
        return None

    async def close_connection(self, conn_id: str) -> None:
        return None


class UnreachableClash:
    """ClashClient whose first call raises a transport error."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def aclose(self) -> None:
        return None

    async def version(self) -> dict[str, Any]:
        raise httpx.ConnectError("connection refused")


# —— Pure parsers ——


def test_parse_groups_filters_group_types_and_enriches_delay() -> None:
    groups = parse_groups(SAMPLE_PROXIES)
    by_name = {g["name"]: g for g in groups}
    # Only Selector/URLTest/Fallback-style groups, never leaf nodes.
    assert set(by_name) == {"GLOBAL", "Auto", "Proxy"}

    global_group = by_name["GLOBAL"]
    assert global_group["type"] == "Selector"
    assert global_group["now"] == "Auto"
    nodes = {n["name"]: n for n in global_group["all"]}
    assert set(nodes) == {"Auto", "Proxy", "HK", "DIRECT"}
    # Delay comes from each member's own latest history entry (0 when absent).
    assert nodes["HK"]["delay"] == 120
    assert nodes["HK"]["type"] == "Shadowsocks"
    assert nodes["Auto"]["delay"] == 88
    assert nodes["DIRECT"]["delay"] == 0
    # A measured delay is never attributed to another proxy.
    assert nodes["HK"]["delay_of"] == ""
    assert nodes["Auto"]["delay_of"] == ""


def test_parse_groups_inherits_delay_from_nested_group_selection() -> None:
    """A group member without history reports the delay of its current node.

    Mihomo leaves ``history`` empty on every group proxy, so without this the
    nested "Proxy" group would render as "—" even though it routes through a
    node with a perfectly good 120 ms measurement.
    """
    nodes = {
        n["name"]: n
        for g in parse_groups(SAMPLE_PROXIES)
        if g["name"] == "GLOBAL"
        for n in g["all"]
    }
    assert nodes["Proxy"]["delay"] == 120
    assert nodes["Proxy"]["delay_of"] == "HK"


def test_parse_groups_survives_cyclic_now_chain() -> None:
    """Mutually-referencing groups resolve to 0 instead of recursing forever."""
    payload = {
        "proxies": {
            "A": {"type": "Selector", "now": "B", "all": ["B"], "history": []},
            "B": {"type": "Selector", "now": "A", "all": ["A"], "history": []},
        }
    }
    groups = {g["name"]: g for g in parse_groups(payload)}
    assert groups["A"]["all"][0] == {"name": "B", "type": "Selector", "delay": 0, "delay_of": ""}


def test_parse_groups_tolerates_malformed_payload() -> None:
    assert parse_groups({}) == []
    assert parse_groups({"proxies": []}) == []
    assert parse_groups({"proxies": {"X": "not-a-dict"}}) == []


def test_parse_status_maps_controller_fields() -> None:
    status = parse_status(
        {"version": "1.18.0", "premium": True},
        {"mode": "global", "mixed-port": 7890, "allow-lan": True, "log-level": "warning"},
    )
    assert status == {
        "version": "1.18.0",
        "mode": "global",
        "mixed_port": 7890,
        "port": 0,
        "socks_port": 0,
        "redir_port": 0,
        "allow_lan": True,
        "log_level": "warning",
    }


# —— ClashClient URL building (httpx.MockTransport, no live controller) ——


async def test_clash_client_builds_urls_and_sends_bearer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "x"})
        if request.url.path.endswith("/delay"):
            return httpx.Response(200, json={"delay": 42})
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with ClashClient("http://127.0.0.1:9090/", "s3cret", transport=transport) as client:
        assert await client.version() == {"version": "x"}
        assert await client.delay("HK 01", timeout_ms=3000) == {"delay": 42}
        await client.select("Proxy Group", "HK 01")

    version_req = seen[0]
    assert version_req.headers["authorization"] == "Bearer s3cret"

    delay_req = seen[1]
    # Path segment is percent-encoded on the wire and decodes back to the raw name.
    assert delay_req.url.path == "/proxies/HK 01/delay"
    assert b"HK%2001" in delay_req.url.raw_path
    assert delay_req.url.params["timeout"] == "3000"
    assert delay_req.url.params["url"] == DELAY_TEST_URL

    select_req = seen[2]
    assert select_req.method == "PUT"
    assert select_req.url.path == "/proxies/Proxy Group"


async def test_clash_client_delay_timeout_and_no_secret() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(408, json={"message": "An error occurred in the delay test"})

    transport = httpx.MockTransport(handler)
    async with ClashClient("http://127.0.0.1:9090", transport=transport) as client:
        result = await client.delay("HK")
    assert result == {"delay": 0, "message": "An error occurred in the delay test"}
    # No secret configured => no Authorization header.
    assert "authorization" not in seen[0].headers


# —— Router: reads ——


async def test_status_returns_parsed_summary(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    resp = await client.get(f"{PROXY}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "mihomo-1.18.0"
    assert body["mode"] == "rule"
    assert body["mixed_port"] == 7890


async def test_groups_returns_selectable_groups(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    resp = await client.get(f"{PROXY}/groups")
    assert resp.status_code == 200
    names = {g["name"] for g in resp.json()["groups"]}
    assert names == {"GLOBAL", "Auto", "Proxy"}


async def test_group_delay_uses_batch_endpoint(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One controller round-trip covers the whole group, using its own testUrl."""
    monkeypatch.setattr(FakeClash, "group_delay_result", {"HK": 120, "JP": 300})
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    resp = await client.get(f"{PROXY}/groups/GLOBAL/delay", params={"timeout": 3000})
    assert resp.status_code == 200
    assert resp.json() == {"delays": {"HK": 120, "JP": 300}, "batched": True}
    assert FakeClash.last_group_probe == (
        "GLOBAL",
        3000,
        "http://cp.cloudflare.com/generate_204",
    )


async def test_group_delay_falls_back_to_per_node_probes(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cores without /group/{name}/delay still get results, one probe per member."""
    monkeypatch.setattr(FakeClash, "group_delay_result", None)
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    resp = await client.get(f"{PROXY}/groups/Auto/delay")
    assert resp.status_code == 200
    body = resp.json()
    assert body["batched"] is False
    # Members that time out (delay 0) are omitted rather than reported as dead.
    assert body["delays"] == {"HK": 120, "JP": 300}


async def test_group_delay_rejects_unknown_group(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    resp = await client.get(f"{PROXY}/groups/HK/delay")
    assert_problem(resp, 404, "not_found")


async def test_status_requires_authentication(client: httpx.AsyncClient) -> None:
    # No owner fixture => no session cookie.
    resp = await client.get(f"{PROXY}/status")
    assert_problem(resp, 401, "auth.required")


async def test_controller_unreachable_maps_to_502(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", UnreachableClash)
    resp = await client.get(f"{PROXY}/status")
    assert_problem(resp, 502, "helper.unavailable")


# —— Router: mutations (CSRF + audit) ——


async def test_select_requires_csrf(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    # No X-CSRF-Token header.
    resp = await client.post(f"{PROXY}/groups/GLOBAL/select", json={"name": "HK"})
    assert_problem(resp, 403, "csrf.invalid")


async def test_select_ok_with_csrf(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    resp = await client.post(
        f"{PROXY}/groups/GLOBAL/select", json={"name": "HK"}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "group": "GLOBAL", "now": "HK"}


async def test_patch_mode_ok(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    resp = await client.patch(
        f"{PROXY}/config", json={"mode": "global"}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "mode": "global"}


async def test_patch_mode_rejects_invalid_value(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    resp = await client.patch(
        f"{PROXY}/config", json={"mode": "bogus"}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 422, "request.validation")


# —— Router: config file (read directly, write behind CSRF + reauth) ——


async def test_config_file_get_returns_text(
    app: Any, client: httpx.AsyncClient, owner: OwnerContext, tmp_path: Any
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mixed-port: 7890\nmode: rule\n", encoding="utf-8")
    app.state.settings.proxy.clash_config = cfg

    resp = await client.get(f"{PROXY}/config-file")
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(cfg)
    assert body["content"].startswith("mixed-port: 7890")


async def test_config_file_put_requires_reauth(
    app: Any, client: httpx.AsyncClient, owner: OwnerContext, tmp_path: Any
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: rule\n", encoding="utf-8")
    app.state.settings.proxy.clash_config = cfg

    resp = await client.put(
        f"{PROXY}/config-file", json={"content": "mode: rule\n"}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 403, "auth.reauth_required")
    # File untouched.
    assert cfg.read_text(encoding="utf-8") == "mode: rule\n"


async def test_config_file_put_writes_and_reloads(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: rule\n", encoding="utf-8")
    app.state.settings.proxy.clash_config = cfg
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow()))

    new_content = "mixed-port: 7891\nmode: global\n"
    resp = await client.put(
        f"{PROXY}/config-file", json={"content": new_content}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "path": str(cfg)}
    assert cfg.read_text(encoding="utf-8") == new_content


async def test_config_file_put_rejects_invalid_yaml(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: rule\n", encoding="utf-8")
    app.state.settings.proxy.clash_config = cfg
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow()))

    resp = await client.put(
        f"{PROXY}/config-file", json={"content": "mode: [unterminated"}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 400, "request.bad")
    # Invalid content never reaches disk (validation precedes the atomic write).
    assert cfg.read_text(encoding="utf-8") == "mode: rule\n"


# —— Custom rules: pure helpers ——


def test_validate_rule_normalises_and_accepts() -> None:
    from mypanel.proxy.custom_rules import validate_rule

    assert validate_rule(" domain-suffix , example.com , PROXY ") == (
        "DOMAIN-SUFFIX,example.com,PROXY"
    )
    assert validate_rule("MATCH,DIRECT") == "MATCH,DIRECT"
    assert validate_rule("IP-CIDR,10.0.0.0/8,DIRECT,no-resolve") == (
        "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "DOMAIN-SUFFIX",  # missing segments
        "MATCH,DIRECT,extra",  # MATCH takes exactly one policy
        "DOMAIN-SUFFIX,,PROXY",  # empty segment
        "DOMAIN-SUFFIX,exa'mple.com,PROXY",  # forbidden quote
        "DOMAIN-SUFFIX,a.com,PROXY,x,y",  # too many segments
        "点类型,a.com,PROXY",  # non-ASCII type
    ],
)
def test_validate_rule_rejects_malformed(bad: str) -> None:
    from mypanel.errors import BadRequestError
    from mypanel.proxy.custom_rules import validate_rule

    with pytest.raises(BadRequestError):
        validate_rule(bad)


def test_inject_rules_prepends_block_and_is_idempotent() -> None:
    import yaml

    from mypanel.proxy.custom_rules import inject_rules, strip_injected

    base = "mode: rule\nrules:\n  - DOMAIN,old.com,DIRECT\n  - MATCH,PROXY\n"
    once = inject_rules(base, ["DOMAIN-SUFFIX,a.com,PROXY"])
    # Custom rules land ahead of the config's own rules.
    parsed = yaml.safe_load(once)
    assert parsed["rules"][0] == "DOMAIN-SUFFIX,a.com,PROXY"
    assert parsed["rules"][1] == "DOMAIN,old.com,DIRECT"
    # Re-injection replaces the previous block instead of stacking.
    twice = inject_rules(once, ["DOMAIN-SUFFIX,b.com,PROXY"])
    parsed = yaml.safe_load(twice)
    assert parsed["rules"][0] == "DOMAIN-SUFFIX,b.com,PROXY"
    assert "a.com" not in twice
    # Stripping restores the original text exactly.
    assert strip_injected(twice) == base


def test_inject_rules_without_rules_key_appends_section() -> None:
    import yaml

    from mypanel.proxy.custom_rules import inject_rules

    out = inject_rules("mode: rule", ["MATCH,DIRECT"])
    assert yaml.safe_load(out)["rules"] == ["MATCH,DIRECT"]


def test_inject_rules_rejects_flow_style() -> None:
    from mypanel.errors import BadRequestError
    from mypanel.proxy.custom_rules import inject_rules

    with pytest.raises(BadRequestError):
        inject_rules("rules: [MATCH,DIRECT]\n", ["DOMAIN,a.com,PROXY"])


# —— Router: clash service on/off ——


async def test_service_status_reports_running(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_systemctl(settings: Any, *args: str) -> tuple[int, str]:
        assert args == ("is-active",)
        return 0, "active"

    monkeypatch.setattr(proxy_router, "_run_systemctl", fake_systemctl)
    resp = await client.get(f"{PROXY}/service")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is True
    assert body["state"] == "active"
    assert body["service"] == "clash.service"


async def test_service_status_requires_authentication(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{PROXY}/service")
    assert_problem(resp, 401, "auth.required")


async def test_service_action_requires_csrf(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.post(f"{PROXY}/service", json={"action": "start"})
    assert_problem(resp, 403, "csrf.invalid")


async def test_service_action_start_ok_and_audited(
    app: Any, client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_systemctl(settings: Any, *args: str) -> tuple[int, str]:
        calls.append(args)
        return 0, "active" if args == ("is-active",) else ""

    monkeypatch.setattr(proxy_router, "_run_systemctl", fake_systemctl)
    resp = await client.post(
        f"{PROXY}/service", json={"action": "start"}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["running"] is True
    assert ("start",) in calls

    from mypanel.db.models import AuditLog

    row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "proxy.service").order_by(AuditLog.ts.desc()),
    )
    assert row is not None
    assert row.result == "ok"
    assert row.target == "start"


async def test_service_action_failure_maps_to_400(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_systemctl(settings: Any, *args: str) -> tuple[int, str]:
        if args == ("stop",):
            return 1, "Failed to stop clash.service: Unit not loaded."
        return 3, "inactive"

    monkeypatch.setattr(proxy_router, "_run_systemctl", fake_systemctl)
    resp = await client.post(
        f"{PROXY}/service", json={"action": "stop"}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 400, "request.bad")


# —— Router: custom rules survive config uploads ——


def _enable_config(
    app: Any, tmp_path: Any, content: str = "mode: rule\nrules:\n  - MATCH,DIRECT\n"
) -> Any:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(content, encoding="utf-8")
    app.state.settings.proxy.clash_config = cfg
    return cfg


async def test_custom_rules_empty_by_default(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.get(f"{PROXY}/rules/custom")
    assert resp.status_code == 200
    assert resp.json() == {"rules": []}


async def test_custom_rule_add_injects_into_config(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    cfg = _enable_config(app, tmp_path)

    resp = await client.post(
        f"{PROXY}/rules/custom",
        json={"rule": "DOMAIN-SUFFIX,example.com,DIRECT"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
    entry = resp.json()["rule"]
    assert entry["rule"] == "DOMAIN-SUFFIX,example.com,DIRECT"

    text = cfg.read_text(encoding="utf-8")
    assert "BEGIN mypanel custom rules" in text
    parsed = yaml.safe_load(text)
    assert parsed["rules"][0] == "DOMAIN-SUFFIX,example.com,DIRECT"
    assert parsed["rules"][-1] == "MATCH,DIRECT"

    # Duplicate is rejected.
    resp = await client.post(
        f"{PROXY}/rules/custom",
        json={"rule": "DOMAIN-SUFFIX,example.com,DIRECT"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 409, "conflict")


async def test_custom_rule_survives_config_upload(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    cfg = _enable_config(app, tmp_path)
    resp = await client.post(
        f"{PROXY}/rules/custom",
        json={"rule": "DOMAIN-SUFFIX,keep.me,REJECT"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200

    # A fresh config upload (no custom rules in the body) must not wipe them.
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow()))
    new_content = "mode: global\nrules:\n  - DOMAIN,new.com,PROXY\n"
    resp = await client.put(
        f"{PROXY}/config-file", json={"content": new_content}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200

    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert parsed["mode"] == "global"
    assert parsed["rules"][0] == "DOMAIN-SUFFIX,keep.me,REJECT"
    assert "DOMAIN,new.com,PROXY" in parsed["rules"]


async def test_custom_rule_delete_removes_from_config(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)
    cfg = _enable_config(app, tmp_path)
    resp = await client.post(
        f"{PROXY}/rules/custom",
        json={"rule": "GEOIP,CN,DIRECT"},
        headers=csrf_headers(owner),
    )
    rule_id = resp.json()["rule"]["id"]

    resp = await client.delete(f"{PROXY}/rules/custom/{rule_id}", headers=csrf_headers(owner))
    assert resp.status_code == 200
    assert "GEOIP,CN,DIRECT" not in cfg.read_text(encoding="utf-8")

    resp = await client.get(f"{PROXY}/rules/custom")
    assert resp.json() == {"rules": []}

    resp = await client.delete(f"{PROXY}/rules/custom/{rule_id}", headers=csrf_headers(owner))
    assert_problem(resp, 404, "not_found")


async def test_custom_rule_add_rejects_invalid(
    app: Any, client: httpx.AsyncClient, owner: OwnerContext, tmp_path: Any
) -> None:
    _enable_config(app, tmp_path)
    resp = await client.post(
        f"{PROXY}/rules/custom", json={"rule": "not a rule"}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 400, "request.bad")


async def test_custom_rule_add_requires_config_path(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    # settings.proxy.clash_config is unset in test settings by default.
    resp = await client.post(
        f"{PROXY}/rules/custom",
        json={"rule": "DOMAIN,a.com,PROXY"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 409, "conflict")


# —— Router: connections (tracker-backed) + traffic history ——


async def test_connections_serves_tracker_snapshot(
    app: Any, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    from datetime import UTC, datetime, timedelta

    from mypanel.proxy.tracker import ProxyTracker

    # A dev machine may run a real controller on the default port and the app's
    # own tracker polls it in the background; swap in a fresh, never-started
    # instance so the endpoint serves exactly the canned ingests below.
    tracker = ProxyTracker(app.state.settings)
    app.state.proxy_tracker = tracker

    t0 = datetime.now(tz=UTC) - timedelta(seconds=2)
    payload = {
        "downloadTotal": 1000,
        "uploadTotal": 500,
        "connections": [
            {
                "id": "c1",
                "metadata": {
                    "network": "tcp",
                    "type": "HTTP",
                    "host": "example.com",
                    "sourceIP": "192.168.1.2",
                    "sourcePort": "50000",
                    "destinationIP": "1.2.3.4",
                    "destinationPort": "443",
                },
                "upload": 100,
                "download": 2000,
                "start": "2026-07-22T00:00:00Z",
                "chains": ["Proxy", "HK"],
                "rule": "DOMAIN-SUFFIX",
                "rulePayload": "example.com",
            },
            {"id": "c2", "metadata": {}, "upload": 1, "download": 1, "chains": []},
        ],
    }
    tracker.ingest_connections(payload, t0)
    # Second poll: c2 disappeared -> closed; counters advanced -> rates.
    payload2 = {
        "downloadTotal": 3000,
        "uploadTotal": 700,
        "connections": [dict(payload["connections"][0], upload=300, download=6000)],
    }
    tracker.ingest_connections(payload2, t0 + timedelta(seconds=2))

    resp = await client.get(f"{PROXY}/connections")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["download_total"] == 3000
    assert len(body["active"]) == 1
    active = body["active"][0]
    assert active["host"] == "example.com"
    assert active["download_rate"] == pytest.approx(2000.0)
    assert len(body["closed"]) == 1
    assert body["closed"][0]["id"] == "c2"
    assert body["closed"][0]["closed_at"]


async def test_close_single_connection(
    client: httpx.AsyncClient, owner: OwnerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    class RecordingClash(FakeClash):
        async def close_connection(self, conn_id: str) -> None:
            closed.append(conn_id)

    monkeypatch.setattr(proxy_router, "ClashClient", RecordingClash)
    resp = await client.delete(f"{PROXY}/connections/abc-123", headers=csrf_headers(owner))
    assert resp.status_code == 200
    assert closed == ["abc-123"]

    resp = await client.delete(f"{PROXY}/connections/abc-123")
    assert_problem(resp, 403, "csrf.invalid")


async def test_traffic_history_endpoint(
    app: Any, client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    from datetime import UTC, datetime, timedelta

    from mypanel.proxy.tracker import ProxyTracker

    # Fresh, never-started tracker: isolate from any real controller (see above).
    tracker = ProxyTracker(app.state.settings)
    app.state.proxy_tracker = tracker

    base = datetime.now(tz=UTC).replace(second=0, microsecond=0) - timedelta(minutes=3)
    for i in range(3):
        tracker.ingest_traffic(1000 + i, 2000, base + timedelta(seconds=i))
    # Crossing into the next minute flushes the completed bucket.
    tracker.ingest_traffic(0, 0, base + timedelta(minutes=1))

    resp = await client.get(f"{PROXY}/traffic/history", params={"minutes": 60})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["samples"]) == 1
    sample = body["samples"][0]
    assert sample["up"] == 1001
    assert sample["down"] == 2000
    assert body["latest"] == {"up": 0, "down": 0}

    resp = await client.get(f"{PROXY}/traffic/history", params={"minutes": 0})
    assert_problem(resp, 422, "request.validation")
