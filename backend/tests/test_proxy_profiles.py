"""Subscription profiles, the extend config/script and the synthesis pipeline.

Covers the proxy feature added on top of docs/code-plan-m2-m5.md: the pure merge
semantics and the QuickJS sandbox in :mod:`ownpanel.proxy.compose`, the on-disk
profile store and download logic in :mod:`ownpanel.proxy.profiles`, the background
refresh loop, and the ``/proxy/profiles`` + ``/proxy/extend`` routes (CSRF and
reauth gates included). No network and no live Clash controller: downloads go
through ``httpx.MockTransport`` and the controller through ``FakeClash``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from conftest import API, OwnerContext, csrf_headers, db_execute, naive_utcnow
from sqlalchemy import update
from test_proxy import FakeClash, assert_problem

from ownpanel.config import Settings
from ownpanel.db.models import Session
from ownpanel.errors import BadRequestError
from ownpanel.proxy import profiles as profile_store
from ownpanel.proxy import router as proxy_router
from ownpanel.proxy.compose import apply_merge, compose_sync, parse_yaml_mapping, run_script
from ownpanel.proxy.scheduler import SubscriptionScheduler

PROXY = f"{API}/proxy"

SUBSCRIPTION = """
mixed-port: 7890
mode: rule
dns:
  enable: false
  listen: 0.0.0.0:53
proxies:
  - {name: HK, type: ss}
proxy-groups:
  - {name: Proxy, type: select, proxies: [HK]}
rules:
  - MATCH,Proxy
"""


# —— compose: merge semantics ——


def test_apply_merge_splices_and_deep_merges() -> None:
    config = {
        "dns": {"enable": False, "listen": "0.0.0.0:53"},
        "rules": ["MATCH,Proxy"],
        "proxies": [{"name": "HK"}],
    }
    merged = apply_merge(
        config,
        {
            "dns": {"enable": True},
            "prepend-rules": ["DOMAIN-SUFFIX,a.com,DIRECT"],
            "append-rules": ["DOMAIN-SUFFIX,z.com,REJECT"],
            "append-proxies": [{"name": "JP"}],
            "allow-lan": True,
        },
    )
    # Deep merge keeps the sibling key the subscription set.
    assert merged["dns"] == {"enable": True, "listen": "0.0.0.0:53"}
    assert merged["rules"] == [
        "DOMAIN-SUFFIX,a.com,DIRECT",
        "MATCH,Proxy",
        "DOMAIN-SUFFIX,z.com,REJECT",
    ]
    assert merged["proxies"] == [{"name": "HK"}, {"name": "JP"}]
    assert merged["allow-lan"] is True


def test_apply_merge_plain_key_replaces_list() -> None:
    merged = apply_merge({"rules": ["MATCH,Proxy"]}, {"rules": ["MATCH,DIRECT"]})
    assert merged["rules"] == ["MATCH,DIRECT"]


def test_apply_merge_splice_applies_after_replacement() -> None:
    # A merge that both replaces `rules` and prepends: the prepend wraps the
    # replacement, not the subscription's original list.
    merged = apply_merge(
        {"rules": ["MATCH,Proxy"]},
        {"rules": ["MATCH,DIRECT"], "prepend-rules": ["DOMAIN,a.com,REJECT"]},
    )
    assert merged["rules"] == ["DOMAIN,a.com,REJECT", "MATCH,DIRECT"]


def test_apply_merge_splices_into_missing_list() -> None:
    merged = apply_merge({}, {"append-rules": ["MATCH,DIRECT"]})
    assert merged["rules"] == ["MATCH,DIRECT"]


def test_apply_merge_ignores_unknown_prefixed_keys() -> None:
    # "prepend-dns" is not a spliceable list, so it stays an ordinary key.
    merged = apply_merge({}, {"prepend-dns": [1]})
    assert merged["prepend-dns"] == [1]


def test_parse_yaml_mapping_rejects_non_mapping() -> None:
    assert parse_yaml_mapping("", what="扩展配置") == {}
    with pytest.raises(BadRequestError):
        parse_yaml_mapping("- a\n- b", what="扩展配置")
    with pytest.raises(BadRequestError):
        parse_yaml_mapping("a: [unterminated", what="扩展配置")


# —— compose: the QuickJS sandbox ——


def test_run_script_transforms_and_captures_console() -> None:
    config, logs = run_script(
        {"proxies": [{"name": "HK"}]},
        """
        function main(config) {
          console.log('nodes', config.proxies.length);
          config['allow-lan'] = true;
          return config;
        }
        """,
    )
    assert config["allow-lan"] is True
    assert logs == ["nodes 1"]


def test_run_script_has_no_host_access() -> None:
    config, _ = run_script(
        {},
        "function main(c) { return {"
        " fetch: typeof fetch, require: typeof require, std: typeof std }; }",
    )
    assert config == {"fetch": "undefined", "require": "undefined", "std": "undefined"}


@pytest.mark.parametrize(
    ("script", "fragment"),
    [
        ("function main(c) { while (true) {} }", "执行失败"),
        ("function main(c) { throw new Error('boom'); }", "boom"),
        ("function main(c) { return 1; }", "必须返回一个对象"),
        ("var x = 1;", "必须定义一个 main"),
        ("function main( {", "编译失败"),
    ],
)
def test_run_script_rejects_bad_scripts(script: str, fragment: str) -> None:
    with pytest.raises(BadRequestError) as exc:
        run_script({}, script, timeout_s=1.0, memory_limit_mb=16)
    assert fragment in str(exc.value)


def test_run_script_reports_user_line_numbers() -> None:
    with pytest.raises(BadRequestError) as exc:
        run_script({}, "function main(c) {\n\n  throw new Error('boom');\n}", timeout_s=1.0)
    message = str(exc.value)
    # The prelude the panel wraps around the script must not shift the numbers,
    # and its own frames must not appear at all.
    assert "第 3 行" in message
    assert "__ownpanel" not in message


def test_compose_sync_runs_the_whole_pipeline() -> None:
    text, logs = compose_sync(
        SUBSCRIPTION,
        "dns: {enable: true}\nappend-rules: ['MATCH,DIRECT']\n",
        "function main(c) { console.log('ok'); c.mode = 'global'; return c; }",
    )
    config = yaml.safe_load(text)
    assert config["dns"] == {"enable": True, "listen": "0.0.0.0:53"}
    assert config["rules"] == ["MATCH,Proxy", "MATCH,DIRECT"]
    assert config["mode"] == "global"
    assert logs == ["ok"]


def test_compose_sync_without_script_skips_the_sandbox() -> None:
    text, logs = compose_sync(SUBSCRIPTION, "", "   ")
    assert yaml.safe_load(text)["mode"] == "rule"
    assert logs == []


# —— profiles: pure helpers ——


def test_parse_userinfo_reads_quota_fields() -> None:
    parsed = profile_store.parse_userinfo("upload=100; download=200; total=300; expire=1700000000")
    assert parsed == {"upload": 100, "download": 200, "total": 300, "expire": 1700000000}
    assert profile_store.parse_userinfo("") == {}


def test_validate_url_requires_http_scheme() -> None:
    assert profile_store.validate_url("  https://a.example/sub  ") == "https://a.example/sub"
    for bad in ("", "file:///etc/passwd", "ftp://a.example"):
        with pytest.raises(BadRequestError):
            profile_store.validate_url(bad)


def test_validate_interval_bounds() -> None:
    assert profile_store.validate_interval(0) == 0
    assert profile_store.validate_interval(60) == 60
    with pytest.raises(BadRequestError):
        profile_store.validate_interval(1)
    with pytest.raises(BadRequestError):
        profile_store.validate_interval(profile_store.MAX_INTERVAL_MIN + 1)


def _profile(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "abc123",
        "name": "sub",
        "url": "https://a.example/sub",
        "user_agent": "",
        "interval_min": 60,
        "active": True,
        "last_updated": "",
        "last_error": "",
        "subscription": {},
    }
    base.update(overrides)
    return base


def test_is_due_covers_never_updated_and_elapsed() -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    # Never fetched → due right away, so a subscription added while offline is
    # retried on the next tick instead of after a whole interval.
    assert profile_store.is_due(_profile(), now) is True
    assert profile_store.is_due(_profile(interval_min=0), now) is False
    fresh = (now - timedelta(minutes=30)).isoformat()
    assert profile_store.is_due(_profile(last_updated=fresh), now) is False
    stale = (now - timedelta(minutes=61)).isoformat()
    assert profile_store.is_due(_profile(last_updated=stale), now) is True
    assert profile_store.is_due(_profile(last_updated="not-a-date"), now) is True


def test_due_profiles_filters(settings: Settings) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    items = [_profile(id="a"), _profile(id="b", interval_min=0)]
    assert [p["id"] for p in profile_store.due_profiles(items, now)] == ["a"]


def test_profile_store_round_trip(settings: Settings) -> None:
    entry = profile_store.new_profile(
        name=" 机场 ", url="https://a.example/sub", interval_min=60, user_agent="", active=True
    )
    profile_store.save_profiles(settings, [entry])
    loaded = profile_store.load_profiles(settings)
    assert [p["id"] for p in loaded] == [entry["id"]]
    assert loaded[0]["name"] == "机场"
    assert profile_store.active_profile(loaded) is not None


def test_load_profiles_tolerates_corrupt_file(settings: Settings) -> None:
    settings.paths.data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    profile_store.profiles_path(settings).write_text("{not json", encoding="utf-8")
    assert profile_store.load_profiles(settings) == []


def test_set_active_is_exclusive() -> None:
    items = [_profile(id="a", active=True), _profile(id="b", active=False)]
    profile_store.set_active(items, "b")
    assert [p["active"] for p in items] == [False, True]


# —— profiles: downloading ——


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    """Route subscription downloads through ``handler``; returns seen requests.

    Also disables the proxy retry: a unit test must not depend on a controller
    being reachable.
    """
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(settings: Settings, user_agent: str, proxy: str = "") -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(record), headers={"User-Agent": user_agent}
        )

    monkeypatch.setattr(profile_store, "make_client", factory)
    monkeypatch.setattr(profile_store, "resolve_fallback_proxy", _no_fallback)
    return seen


async def _no_fallback(settings: Settings) -> str:
    return ""


async def test_fetch_subscription_reads_body_and_userinfo(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _mock_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            text=SUBSCRIPTION,
            headers={"subscription-userinfo": "upload=1; download=2; total=3"},
        ),
    )
    text, info = await profile_store.fetch_subscription(settings, _profile())
    assert "mixed-port" in text
    assert info == {"upload": 1, "download": 2, "total": 3}
    assert seen[0].headers["user-agent"] == settings.proxy.subscription_user_agent


async def test_fetch_subscription_uses_profile_user_agent(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _mock_client(monkeypatch, lambda request: httpx.Response(200, text=SUBSCRIPTION))
    await profile_store.fetch_subscription(settings, _profile(user_agent="my-client/1.0"))
    assert seen[0].headers["user-agent"] == "my-client/1.0"


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (httpx.Response(404, text="nope"), "HTTP 404"),
        (httpx.Response(200, text="   "), "内容为空"),
        (httpx.Response(200, content=b"\xff\xfe\x00bad"), "UTF-8"),
    ],
)
async def test_fetch_subscription_rejects_bad_responses(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    fragment: str,
) -> None:
    _mock_client(monkeypatch, lambda request: response)
    with pytest.raises(BadRequestError) as exc:
        await profile_store.fetch_subscription(settings, _profile())
    assert fragment in str(exc.value)


async def test_fetch_subscription_rejects_oversized_body(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversized = b"x" * (profile_store.MAX_SUBSCRIPTION_BYTES + 1)
    _mock_client(monkeypatch, lambda request: httpx.Response(200, content=oversized))
    with pytest.raises(BadRequestError) as exc:
        await profile_store.fetch_subscription(settings, _profile())
    assert "上限" in str(exc.value)


async def test_fetch_subscription_maps_transport_errors(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_client(monkeypatch, boom)
    with pytest.raises(BadRequestError) as exc:
        await profile_store.fetch_subscription(settings, _profile())
    assert "下载订阅失败" in str(exc.value)


async def test_fetch_subscription_names_errors_that_stringify_empty(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        # httpx.ConnectTimeout carries no message here — the most common real
        # failure would otherwise render as a bare "下载订阅失败: ".
        raise httpx.ConnectTimeout("")

    _mock_client(monkeypatch, timeout)
    with pytest.raises(BadRequestError) as exc:
        await profile_store.fetch_subscription(settings, _profile())
    assert "ConnectTimeout" in str(exc.value)


# —— profiles: direct first, proxy retry on failure ——


async def test_resolve_fallback_proxy_discovers_the_core_listen_port(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Airport dashboards are often blocked on a direct connection, so "auto"
    # must turn the controller's own mixed-port into the download proxy.
    monkeypatch.setattr(profile_store, "ClashClient", FakeClash)
    settings.proxy.subscription_fallback_proxy = "auto"
    assert await profile_store.resolve_fallback_proxy(settings) == "http://127.0.0.1:7890"


async def test_resolve_fallback_proxy_honours_explicit_and_off_settings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile_store, "ClashClient", FakeClash)
    settings.proxy.subscription_fallback_proxy = "http://127.0.0.1:1080"
    assert await profile_store.resolve_fallback_proxy(settings) == "http://127.0.0.1:1080"
    for direct in ("none", "", "  Direct "):
        settings.proxy.subscription_fallback_proxy = direct
        assert await profile_store.resolve_fallback_proxy(settings) == ""


async def test_resolve_fallback_proxy_is_empty_when_core_is_down(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DownClash(FakeClash):
        async def configs(self) -> dict[str, Any]:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(profile_store, "ClashClient", DownClash)
    settings.proxy.subscription_fallback_proxy = "auto"
    assert await profile_store.resolve_fallback_proxy(settings) == ""


async def test_resolve_fallback_proxy_is_empty_without_a_listen_port(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PortlessClash(FakeClash):
        async def configs(self) -> dict[str, Any]:
            return {"mode": "rule", "mixed-port": 0, "port": 0}

    monkeypatch.setattr(profile_store, "ClashClient", PortlessClash)
    settings.proxy.subscription_fallback_proxy = "auto"
    assert await profile_store.resolve_fallback_proxy(settings) == ""


async def test_fetch_subscription_prefers_a_direct_connection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[str] = []

    def factory(settings: Settings, user_agent: str, proxy: str = "") -> httpx.AsyncClient:
        used.append(proxy)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=SUBSCRIPTION))
        )

    monkeypatch.setattr(profile_store, "make_client", factory)
    monkeypatch.setattr(profile_store, "ClashClient", FakeClash)
    settings.proxy.subscription_fallback_proxy = "auto"

    # Direct is the fast path and the only one a fresh install can use, so a
    # working direct connection must never be preceded by a proxy attempt.
    await profile_store.fetch_subscription(settings, _profile())
    assert used == [""]


async def test_fetch_subscription_retries_through_the_proxy_when_direct_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[str] = []

    def factory(settings: Settings, user_agent: str, proxy: str = "") -> httpx.AsyncClient:
        used.append(proxy)

        def handle(request: httpx.Request) -> httpx.Response:
            # Stands in for a blocked airport dashboard: only the proxy gets through.
            if not proxy:
                raise httpx.ConnectTimeout("connect timeout")
            return httpx.Response(200, text=SUBSCRIPTION)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(profile_store, "make_client", factory)
    monkeypatch.setattr(profile_store, "ClashClient", FakeClash)
    settings.proxy.subscription_fallback_proxy = "auto"

    text, _ = await profile_store.fetch_subscription(settings, _profile())
    assert "mixed-port" in text
    assert used == ["", "http://127.0.0.1:7890"]


async def test_fetch_subscription_skips_the_retry_when_disabled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[str] = []

    def factory(settings: Settings, user_agent: str, proxy: str = "") -> httpx.AsyncClient:
        used.append(proxy)

        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timeout")

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(profile_store, "make_client", factory)
    settings.proxy.subscription_fallback_proxy = "none"

    with pytest.raises(BadRequestError) as exc:
        await profile_store.fetch_subscription(settings, _profile())
    # The original direct error surfaces unchanged, not a combined message.
    assert "下载订阅失败" in str(exc.value)
    assert used == [""]


async def test_fetch_subscription_reports_both_attempts_when_both_fail(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(settings: Settings, user_agent: str, proxy: str = "") -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("proxy refused" if proxy else "connect timeout")

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(profile_store, "make_client", factory)
    monkeypatch.setattr(profile_store, "ClashClient", FakeClash)
    settings.proxy.subscription_fallback_proxy = "auto"

    with pytest.raises(BadRequestError) as exc:
        await profile_store.fetch_subscription(settings, _profile())
    message = str(exc.value)
    assert "直连失败" in message
    assert "经代理" in message


# —— profiles: applying the pipeline output ——


def _configured(settings: Settings, tmp_path: Path) -> Path:
    cfg = tmp_path / "clash.yaml"
    cfg.write_text("mode: rule\n", encoding="utf-8")
    settings.proxy.clash_config = cfg
    return cfg


async def test_apply_profile_writes_and_keeps_custom_rules(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ownpanel.proxy.custom_rules import new_rule_entry, save_rules

    monkeypatch.setattr(profile_store, "ClashClient", FakeClash)
    cfg = _configured(settings, tmp_path)
    entry = _profile()
    profile_store.store_raw(settings, entry["id"], SUBSCRIPTION)
    save_rules(settings, [new_rule_entry("DOMAIN-SUFFIX,keep.me,DIRECT")])
    profile_store.save_extend(settings, merge="allow-lan: true\n", script="")

    path, logs = await profile_store.apply_profile(settings, entry)
    assert path == cfg
    assert logs == []
    written = cfg.read_text(encoding="utf-8")
    config = yaml.safe_load(written)
    assert config["allow-lan"] is True
    # The panel-managed rule survives regeneration and keeps top priority.
    assert config["rules"][0] == "DOMAIN-SUFFIX,keep.me,DIRECT"


async def test_apply_profile_requires_downloaded_content(
    settings: Settings, tmp_path: Path
) -> None:
    _configured(settings, tmp_path)
    with pytest.raises(BadRequestError):
        await profile_store.apply_profile(settings, _profile())


async def test_write_and_reload_rolls_back_on_controller_failure(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RefusingClash(FakeClash):
        async def reload_configs(self, *, path: str) -> None:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(profile_store, "ClashClient", RefusingClash)
    cfg = _configured(settings, tmp_path)
    with pytest.raises(Exception) as exc:
        await profile_store.write_and_reload(settings, "mode: global\n")
    assert exc.value.__class__.__name__ == "HelperUnavailableError"
    # The controller still runs the old config, so the file must match it again.
    assert cfg.read_text(encoding="utf-8") == "mode: rule\n"


# —— scheduler ——


async def test_scheduler_tick_refreshes_due_and_applies_active(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile_store, "ClashClient", FakeClash)
    _mock_client(monkeypatch, lambda request: httpx.Response(200, text=SUBSCRIPTION))
    cfg = _configured(settings, tmp_path)
    profile_store.save_profiles(
        settings, [_profile(id="aaa", active=True), _profile(id="bbb", interval_min=0)]
    )

    assert await SubscriptionScheduler(settings).tick() == 1
    stored = {p["id"]: p for p in profile_store.load_profiles(settings)}
    assert stored["aaa"]["last_updated"]
    assert stored["bbb"]["last_updated"] == ""
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["mode"] == "rule"


async def test_scheduler_tick_records_failure_without_raising(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_client(monkeypatch, boom)
    profile_store.save_profiles(settings, [_profile(id="aaa")])

    assert await SubscriptionScheduler(settings).tick() == 0
    stored = profile_store.load_profiles(settings)[0]
    assert "下载订阅失败" in stored["last_error"]
    assert stored["last_updated"] == ""


async def test_scheduler_start_stop_is_prompt(settings: Settings) -> None:
    scheduler = SubscriptionScheduler(settings)
    await scheduler.start()
    # stop() must not wait out the poll interval: the closing event wakes _pause.
    await scheduler.stop()


# —— router: profiles ——


@pytest.fixture
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_client(monkeypatch, lambda request: httpx.Response(200, text=SUBSCRIPTION))
    monkeypatch.setattr(profile_store, "ClashClient", FakeClash)
    monkeypatch.setattr(proxy_router, "ClashClient", FakeClash)


async def _add_profile(
    client: httpx.AsyncClient, owner: OwnerContext, **body: Any
) -> dict[str, Any]:
    payload = {"name": "sub", "url": "https://a.example/sub", "interval_min": 0, **body}
    resp = await client.post(f"{PROXY}/profiles", json=payload, headers=csrf_headers(owner))
    assert resp.status_code == 200, resp.text
    profile: dict[str, Any] = resp.json()["profile"]
    return profile


async def test_profiles_list_is_empty_initially(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.get(f"{PROXY}/profiles")
    assert resp.status_code == 200
    assert resp.json() == {"profiles": [], "active_id": ""}


async def test_add_profile_downloads_activates_and_applies(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    _no_network: None,
) -> None:
    cfg = _configured(app.state.settings, tmp_path)
    profile = await _add_profile(client, owner)
    # The first profile becomes active, so the config file is regenerated now.
    assert profile["active"] is True
    assert profile["last_updated"]
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["mixed-port"] == 7890

    resp = await client.get(f"{PROXY}/profiles")
    assert resp.json()["active_id"] == profile["id"]


async def test_add_second_profile_does_not_steal_active(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    _no_network: None,
) -> None:
    _configured(app.state.settings, tmp_path)
    first = await _add_profile(client, owner)
    second = await _add_profile(client, owner, name="other")
    assert second["active"] is False

    resp = await client.post(
        f"{PROXY}/profiles/{second['id']}/activate", headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    assert resp.json()["active_id"] == second["id"]
    assert {p["id"]: p["active"] for p in resp.json()["profiles"]}[first["id"]] is False


async def test_add_profile_requires_csrf(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.post(f"{PROXY}/profiles", json={"url": "https://a.example/sub"})
    assert_problem(resp, 403, "csrf.invalid")


async def test_add_profile_rejects_non_http_url(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(
        f"{PROXY}/profiles", json={"url": "file:///etc/passwd"}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 400, "request.bad")
    assert profile_store.load_profiles.__module__  # sanity: nothing stored
    resp = await client.get(f"{PROXY}/profiles")
    assert resp.json()["profiles"] == []


async def test_update_profile_edits_fields(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    _no_network: None,
) -> None:
    _configured(app.state.settings, tmp_path)
    profile = await _add_profile(client, owner)
    resp = await client.patch(
        f"{PROXY}/profiles/{profile['id']}",
        json={"name": "renamed", "interval_min": 120},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
    assert resp.json()["profile"]["name"] == "renamed"
    assert resp.json()["profile"]["interval_min"] == 120


async def test_update_profile_rejects_bad_interval(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    _no_network: None,
) -> None:
    _configured(app.state.settings, tmp_path)
    profile = await _add_profile(client, owner)
    resp = await client.patch(
        f"{PROXY}/profiles/{profile['id']}",
        json={"interval_min": 1},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 400, "request.bad")


async def test_delete_profile_removes_cache_but_keeps_config(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    _no_network: None,
) -> None:
    settings = app.state.settings
    cfg = _configured(settings, tmp_path)
    profile = await _add_profile(client, owner)
    before = cfg.read_text(encoding="utf-8")

    resp = await client.delete(f"{PROXY}/profiles/{profile['id']}", headers=csrf_headers(owner))
    assert resp.status_code == 200
    assert resp.json() == {"profiles": [], "active_id": ""}
    assert not profile_store.raw_path(settings, profile["id"]).exists()
    # Deleting a subscription must not take the running proxy offline.
    assert cfg.read_text(encoding="utf-8") == before


async def test_unknown_profile_is_404(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.delete(f"{PROXY}/profiles/deadbeef", headers=csrf_headers(owner))
    assert_problem(resp, 404, "not_found")


async def test_refresh_profile_records_failure(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_network: None,
) -> None:
    _configured(app.state.settings, tmp_path)
    profile = await _add_profile(client, owner)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_client(monkeypatch, boom)
    resp = await client.post(
        f"{PROXY}/profiles/{profile['id']}/update", headers=csrf_headers(owner)
    )
    assert_problem(resp, 400, "request.bad")

    resp = await client.get(f"{PROXY}/profiles")
    assert "下载订阅失败" in resp.json()["profiles"][0]["last_error"]


# —— router: extend config/script ——


async def test_extend_is_empty_initially(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.get(f"{PROXY}/extend")
    assert resp.status_code == 200
    assert resp.json() == {"merge": "", "script": ""}


async def test_put_extend_requires_reauth(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.put(
        f"{PROXY}/extend", json={"merge": "allow-lan: true\n"}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 403, "auth.reauth_required")


async def test_put_extend_saves_and_regenerates(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    _no_network: None,
) -> None:
    cfg = _configured(app.state.settings, tmp_path)
    await _add_profile(client, owner)
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow()))

    resp = await client.put(
        f"{PROXY}/extend",
        json={
            "merge": "allow-lan: true\n",
            "script": "function main(c){ c.mode='global'; return c }",
        },
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
    config = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert config["allow-lan"] is True
    assert config["mode"] == "global"


async def test_put_extend_rejects_broken_script_without_persisting(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    _no_network: None,
) -> None:
    cfg = _configured(app.state.settings, tmp_path)
    await _add_profile(client, owner)
    before = cfg.read_text(encoding="utf-8")
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow()))

    resp = await client.put(
        f"{PROXY}/extend",
        json={"script": "function main(c){ throw new Error('nope') }"},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 400, "request.bad")
    # A script that cannot run must not become the state every later update replays.
    assert (await client.get(f"{PROXY}/extend")).json()["script"] == ""
    assert cfg.read_text(encoding="utf-8") == before


async def test_preview_extend_does_not_write(
    app: Any,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    tmp_path: Path,
    _no_network: None,
) -> None:
    cfg = _configured(app.state.settings, tmp_path)
    await _add_profile(client, owner)
    before = cfg.read_text(encoding="utf-8")

    resp = await client.post(
        f"{PROXY}/extend/preview",
        json={
            "merge": "allow-lan: true\n",
            "script": "function main(c){ console.log('hi'); return c }",
        },
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert yaml.safe_load(body["content"])["allow-lan"] is True
    assert body["logs"] == ["hi"]
    assert cfg.read_text(encoding="utf-8") == before
    assert (await client.get(f"{PROXY}/extend")).json()["merge"] == ""


async def test_preview_extend_without_profile_is_conflict(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(f"{PROXY}/extend/preview", json={}, headers=csrf_headers(owner))
    assert_problem(resp, 409, "conflict")
