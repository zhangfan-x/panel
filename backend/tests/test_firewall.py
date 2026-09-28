"""Firewall (ufw) domain tests.

**Nothing here touches the host firewall.** The parsers run against captured
``ufw status`` text, argument validation never spawns a process, and every
action test replaces :func:`ownpanel.helper.actions.ufw._ufw` with a fake that
records the argv it was handed. Because the helper server runs in-process (a
thread, see ``test_helper.running_helper``) that patch is visible to its
handler threads too, so the API-level tests are equally inert.

The sample output below is real ``ufw status`` text from a live host (English
forced via ``LANGUAGE=C``): it exercises app-profile rules whose ``To`` column
is a name with spaces, ``(v6)`` duplicates, CIDR sources and trailing comments.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    API,
    OwnerContext,
    SettingsFactory,
    csrf_headers,
    db_execute,
    db_scalar,
    naive_utcnow,
)
from fastapi import FastAPI
from sqlalchemy import select, update
from test_helper import running_helper

from ownpanel.config import Settings
from ownpanel.db.models import AuditLog, Session
from ownpanel.helper.actions import ufw
from ownpanel.helper.registry import ActionError
from ownpanel.ipc.client import HelperClient
from ownpanel.website.firewall import (
    covered_ports,
    endangered,
    newly_endangered,
    protected_ports,
    rule_ports,
)

importlib.import_module("ownpanel.helper.actions")

FIREWALL = f"{API}/website/firewall"

NUMBERED_SAMPLE = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 3389/tcp                   ALLOW IN    Anywhere
[ 2] 22/tcp                     ALLOW IN    Anywhere
[ 3] 30010/tcp                  ALLOW IN    172.18.0.0/16
[ 4] Nginx Full                 ALLOW IN    Anywhere
[ 5] 39222/tcp                  ALLOW IN    Anywhere                   # sshd alt port
[ 6] 25/tcp                     DENY OUT    Anywhere
[ 7] 6000:6010/udp              ALLOW IN    10.0.0.0/8
[ 8] 22/tcp (v6)                ALLOW IN    Anywhere (v6)
"""

VERBOSE_ALLOW_IN_SAMPLE = """Status: active
Logging: on (low)
Default: allow (incoming), allow (outgoing), deny (routed)
New profiles: skip
"""

VERBOSE_SAMPLE = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), deny (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
"""


class UfwRecorder:
    """Stand-in for the real ufw binary: records argv, replays canned output."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.verbose = VERBOSE_SAMPLE
        self.numbered = NUMBERED_SAMPLE

    def __call__(self, *args: str, timeout_s: float = 0.0) -> str:
        self.calls.append(list(args))
        if args[:1] == ("status",):
            return self.numbered if "numbered" in args else self.verbose
        return ""


@pytest.fixture
def fake_ufw(monkeypatch: pytest.MonkeyPatch) -> UfwRecorder:
    """Replace the ufw invoker so no test can reach the host firewall."""
    recorder = UfwRecorder()
    monkeypatch.setattr(ufw, "_ufw", recorder)
    monkeypatch.setattr(ufw, "_ssh_ports", lambda: [22, 39222])
    # Point the "is ufw installed?" probe at a path that always exists, so the
    # suite behaves identically on hosts with and without ufw.
    monkeypatch.setattr(ufw, "_UFW", "/bin/sh")
    return recorder


@pytest.fixture(scope="module")
def helper_socket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("firewall-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture
def settings(make_settings: SettingsFactory, helper_socket: Path) -> Settings:
    return make_settings(paths={"helper_socket": helper_socket})


async def reauth(app: FastAPI) -> None:
    await db_execute(app, update(Session).values(reauth_at=naive_utcnow()))


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_parse_rules_handles_every_real_world_shape() -> None:
    rules = {r["num"]: r for r in ufw.parse_rules(NUMBERED_SAMPLE)}
    assert len(rules) == 8

    assert rules[2]["to"] == "22/tcp"
    assert rules[2]["action"] == "ALLOW IN"
    assert rules[2]["from"] == "Anywhere"
    assert rules[2]["v6"] is False

    # App profile: the To column is a bare name containing a space.
    assert rules[4]["to"] == "Nginx Full"
    # Trailing comment is split off, not left glued to the From column.
    assert rules[5]["from"] == "Anywhere"
    assert rules[5]["comment"] == "sshd alt port"
    # CIDR source and outbound rules survive intact.
    assert rules[3]["from"] == "172.18.0.0/16"
    assert rules[6]["action"] == "DENY OUT"
    # The (v6) twin is flagged rather than mistaken for a separate port.
    assert rules[8]["v6"] is True


def test_parse_rules_skips_noise() -> None:
    assert ufw.parse_rules("Status: inactive") == []
    assert ufw.parse_rules("") == []


def test_parse_verbose_reads_status_and_defaults() -> None:
    summary = ufw.parse_verbose(VERBOSE_SAMPLE)
    assert summary["active"] is True
    assert summary["logging"] == "on (low)"
    assert summary["default_incoming"] == "deny"
    assert summary["default_outgoing"] == "allow"
    assert summary["default_routed"] == "deny"


def test_parse_verbose_reports_inactive() -> None:
    assert ufw.parse_verbose("Status: inactive\n")["active"] is False


def test_rule_fingerprint_ignores_the_index() -> None:
    """Indices shift on every change; the fingerprint must not depend on them."""
    rules = {r["num"]: r for r in ufw.parse_rules(NUMBERED_SAMPLE)}
    assert ufw.rule_fingerprint(rules[2]) == "22/tcp|ALLOW IN|Anywhere"


# ---------------------------------------------------------------------------
# Argument validation (no subprocess involved)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "port",
    ["0", "65536", "abc", "22;rm -rf /", "22 22", "-1", "6010:6000", "1:2:3"],
)
def test_add_rule_rejects_bad_ports(port: str) -> None:
    with pytest.raises(ValueError, match="端口"):
        ufw.AddRuleArgs(action="allow", port=port)


@pytest.mark.parametrize("port", ["22", "65535", "6000:6010"])
def test_add_rule_accepts_valid_ports(port: str) -> None:
    assert ufw.AddRuleArgs(action="allow", port=port).port == port


@pytest.mark.parametrize("addr", ["not-an-ip", "10.0.0.0/99", "1.2.3.4 5.6.7.8", "$(whoami)"])
def test_add_rule_rejects_bad_addresses(addr: str) -> None:
    with pytest.raises(ValueError, match="来源/目标"):
        ufw.AddRuleArgs(action="allow", port="22", from_addr=addr)


def test_add_rule_normalises_any_address() -> None:
    assert ufw.AddRuleArgs(action="allow", port="22", from_addr="").from_addr == "any"
    assert ufw.AddRuleArgs(action="allow", port="22", from_addr="ANY").from_addr == "any"
    assert ufw.AddRuleArgs(action="allow", port="22", from_addr="10.0.0.0/8").from_addr


@pytest.mark.parametrize("comment", ["a'b", 'a"b', "a\nb", "a`b", "a$b", "a;b"])
def test_add_rule_rejects_shell_metacharacters_in_comments(comment: str) -> None:
    with pytest.raises(ValueError, match="备注"):
        ufw.AddRuleArgs(action="allow", port="22", comment=comment)


def test_add_rule_allows_chinese_comments() -> None:
    assert ufw.AddRuleArgs(action="allow", port="22", comment="面板端口").comment == "面板端口"


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_add_rule_builds_an_argv_array(fake_ufw: UfwRecorder) -> None:
    ufw.ufw_add_rule(
        ufw.AddRuleArgs(
            action="allow",
            direction="in",
            port="8443",
            proto="tcp",
            from_addr="10.0.0.0/8",
            comment="panel",
        )
    )
    assert fake_ufw.calls == [
        [
            "allow",
            "in",
            "from",
            "10.0.0.0/8",
            "to",
            "any",
            "port",
            "8443",
            "proto",
            "tcp",
            "comment",
            "panel",
        ]
    ]


def test_add_rule_omits_empty_optional_fields(fake_ufw: UfwRecorder) -> None:
    ufw.ufw_add_rule(ufw.AddRuleArgs(action="deny", direction="out", port="25"))
    assert fake_ufw.calls == [["deny", "out", "from", "any", "to", "any", "port", "25"]]


def test_add_rule_refuses_a_blanket_rule(fake_ufw: UfwRecorder) -> None:
    """No port and no source would allow (or block) absolutely everything."""
    with pytest.raises(ActionError) as excinfo:
        ufw.ufw_add_rule(ufw.AddRuleArgs(action="allow", direction="in"))
    assert excinfo.value.code == "helper.invalid_args"
    assert fake_ufw.calls == []


def test_enable_forces_past_the_interactive_prompt(fake_ufw: UfwRecorder) -> None:
    ufw.ufw_enable(ufw.EmptyArgs())
    assert fake_ufw.calls == [["--force", "enable"]]


def test_set_default_orders_policy_before_direction(fake_ufw: UfwRecorder) -> None:
    ufw.ufw_set_default(ufw.SetDefaultArgs(direction="incoming", policy="deny"))
    assert fake_ufw.calls == [["default", "deny", "incoming"]]


def test_delete_rule_checks_the_fingerprint_first(fake_ufw: UfwRecorder) -> None:
    ufw.ufw_delete_rule(ufw.DeleteRuleArgs(num=2, expect="22/tcp|ALLOW IN|Anywhere"))
    assert fake_ufw.calls[-1] == ["--force", "delete", "2"]


def test_delete_rule_refuses_when_the_index_drifted(fake_ufw: UfwRecorder) -> None:
    """ufw renumbers on every change, so a stale index must not delete blindly."""
    with pytest.raises(ActionError) as excinfo:
        ufw.ufw_delete_rule(ufw.DeleteRuleArgs(num=2, expect="3389/tcp|ALLOW IN|Anywhere"))
    assert excinfo.value.code == "helper.conflict"
    assert ["--force", "delete", "2"] not in fake_ufw.calls


def test_delete_rule_reports_a_missing_index(fake_ufw: UfwRecorder) -> None:
    with pytest.raises(ActionError) as excinfo:
        ufw.ufw_delete_rule(ufw.DeleteRuleArgs(num=99, expect="x|y|z"))
    assert excinfo.value.code == "helper.not_found"


def test_status_merges_both_ufw_calls(fake_ufw: UfwRecorder) -> None:
    """verbose and numbered are separate ufw invocations; both are needed."""
    result = ufw.ufw_status(ufw.EmptyArgs())
    assert ["status", "verbose"] in fake_ufw.calls
    assert ["status", "numbered"] in fake_ufw.calls
    assert result["installed"] is True
    assert result["active"] is True
    assert result["default_incoming"] == "deny"
    assert len(result["rules"]) == 8
    assert result["ssh_ports"] == [22, 39222]


# ---------------------------------------------------------------------------
# Lock-out analysis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("22/tcp", {22}),
        ("22", {22}),
        ("80,443/tcp", {80, 443}),
        ("22/tcp (v6)", {22}),
        ("6000:6002/udp", {6000, 6001, 6002}),
        # Unreadable specs must be None, never an empty set: an empty set would
        # read as "covers nothing" and an app profile might cover everything.
        ("Nginx Full", None),
        ("Anywhere", None),
        ("Anywhere (v6)", None),
    ],
)
def test_rule_ports_reads_or_admits_defeat(spec: str, expected: set[int] | None) -> None:
    assert rule_ports(spec) == expected


def test_covered_ports_ignores_outbound_and_deny_rules() -> None:
    rules: list[dict[str, Any]] = [
        {"to": "22/tcp", "action": "ALLOW IN", "from": "Anywhere"},
        {"to": "25/tcp", "action": "DENY OUT", "from": "Anywhere"},
        {"to": "8080/tcp", "action": "DENY IN", "from": "Anywhere"},
        {"to": "443/tcp", "action": "ALLOW OUT", "from": "Anywhere"},
    ]
    assert covered_ports(rules) == {22}


def test_endangered_flags_ports_losing_their_last_rule() -> None:
    rules = ufw.parse_rules(NUMBERED_SAMPLE)
    protected = protected_ports(25443, [22, 39222])
    assert protected == [22, 25443, 39222]
    # 25443 has no rule in the sample at all.
    assert endangered(rules, protected) == [25443]
    # Dropping the IPv4 22/tcp rule strands SSH even though a (v6) twin remains.
    without_ssh = [r for r in rules if r["num"] != 2]
    assert endangered(without_ssh, protected) == [22, 25443]


def test_ipv6_twins_do_not_vouch_for_ipv4_access() -> None:
    """Deleting the IPv4 rule while keeping ``22/tcp (v6)`` still cuts off IPv4."""
    rules: list[dict[str, Any]] = [
        {"to": "22/tcp (v6)", "action": "ALLOW IN", "from": "Anywhere (v6)", "v6": True}
    ]
    assert covered_ports(rules) == set()
    assert endangered(rules, [22]) == [22]


def test_newly_endangered_ignores_ports_that_were_already_uncovered() -> None:
    """Only the delta may block: an unrelated deletion must not inherit old gaps.

    The panel port routinely has no rule (reverse-proxied), and blocking every
    deletion on it would train the user to click through the warning.
    """
    rules = ufw.parse_rules(NUMBERED_SAMPLE)
    protected = protected_ports(25443, [22, 39222])
    # Removing 3389/tcp touches no protected port, though 25443 stays uncovered.
    without_rdp = [r for r in rules if r["num"] != 1]
    assert newly_endangered(rules, without_rdp, protected) == []
    # Removing SSH does strand a port that was previously covered.
    without_ssh = [r for r in rules if r["num"] != 2]
    assert newly_endangered(rules, without_ssh, protected) == [22]


def test_app_profile_rules_never_count_as_cover() -> None:
    """`Nginx Full` hides its ports, so it must not vouch for 80/443."""
    rules: list[dict[str, Any]] = [{"to": "Nginx Full", "action": "ALLOW IN", "from": "Anywhere"}]
    assert endangered(rules, [80, 443]) == [80, 443]


# ---------------------------------------------------------------------------
# API gates
# ---------------------------------------------------------------------------


async def test_status_requires_authentication(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{FIREWALL}/status")
    assert resp.status_code == 401


async def test_mutations_require_csrf(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    for method, url, body in (
        ("post", f"{FIREWALL}/enable", {}),
        ("post", f"{FIREWALL}/disable", None),
        ("post", f"{FIREWALL}/rules", {"action": "allow", "port": "22"}),
        ("post", f"{FIREWALL}/default", {"direction": "incoming", "policy": "deny"}),
    ):
        resp = await getattr(client, method)(url, json=body)
        assert resp.status_code == 403, url
        assert resp.json()["code"] == "csrf.invalid"


async def test_enable_requires_recent_reauth(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.post(f"{FIREWALL}/enable", json={}, headers=csrf_headers(owner))
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.reauth_required"


async def test_delete_requires_recent_reauth(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    resp = await client.request(
        "DELETE",
        f"{FIREWALL}/rules/2",
        json={"expect": "22/tcp|ALLOW IN|Anywhere"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.reauth_required"


# ---------------------------------------------------------------------------
# API behaviour (helper faked, host firewall untouched)
# ---------------------------------------------------------------------------


async def test_status_reports_protected_and_uncovered_ports(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, fake_ufw: UfwRecorder
) -> None:
    resp = await client.get(f"{FIREWALL}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["installed"] is True
    assert body["active"] is True
    assert 22 in body["protected_ports"]
    assert 39222 in body["protected_ports"]
    assert len(body["rules"]) == 8


async def test_enable_is_blocked_when_it_would_lock_you_out(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_ufw: UfwRecorder,
) -> None:
    """The panel's own port has no allow rule in the sample, so enabling is refused."""
    await reauth(app)
    resp = await client.post(f"{FIREWALL}/enable", json={}, headers=csrf_headers(owner))
    assert resp.status_code == 409
    assert "无法再连接本机" in resp.json()["detail"]
    assert ["--force", "enable"] not in fake_ufw.calls

    row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "firewall.enable", AuditLog.result == "denied"),
    )
    assert row is not None


async def test_enable_proceeds_once_the_risk_is_acknowledged(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_ufw: UfwRecorder,
) -> None:
    await reauth(app)
    resp = await client.post(
        f"{FIREWALL}/enable", json={"confirm_lockout": True}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200, resp.text
    assert ["--force", "enable"] in fake_ufw.calls


async def test_deleting_the_ssh_rule_is_blocked_without_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_ufw: UfwRecorder,
) -> None:
    await reauth(app)
    resp = await client.request(
        "DELETE",
        f"{FIREWALL}/rules/2",
        json={"expect": "22/tcp|ALLOW IN|Anywhere"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 409
    assert "22" in resp.json()["detail"]
    assert ["--force", "delete", "2"] not in fake_ufw.calls


async def test_deleting_an_unrelated_rule_needs_no_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_ufw: UfwRecorder,
) -> None:
    """Rule 1 (3389/tcp) guards no protected port, so it deletes straight away."""
    await reauth(app)
    resp = await client.request(
        "DELETE",
        f"{FIREWALL}/rules/1",
        json={"expect": "3389/tcp|ALLOW IN|Anywhere"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
    assert ["--force", "delete", "1"] in fake_ufw.calls


async def test_add_rule_is_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_ufw: UfwRecorder,
) -> None:
    resp = await client.post(
        f"{FIREWALL}/rules",
        json={"action": "allow", "direction": "in", "port": "25443", "proto": "tcp"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
    row = await db_scalar(app, select(AuditLog).where(AuditLog.action == "firewall.rule_add"))
    assert row is not None
    assert row.result == "ok"


async def test_tightening_the_incoming_default_is_guarded(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_ufw: UfwRecorder,
) -> None:
    """allow → deny newly blocks every uncovered port, so it needs confirmation."""
    fake_ufw.verbose = VERBOSE_ALLOW_IN_SAMPLE
    await reauth(app)
    resp = await client.post(
        f"{FIREWALL}/default",
        json={"direction": "incoming", "policy": "deny"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 409
    assert ["default", "deny", "incoming"] not in fake_ufw.calls

    resp = await client.post(
        f"{FIREWALL}/default",
        json={"direction": "incoming", "policy": "deny", "confirm_lockout": True},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
    assert ["default", "deny", "incoming"] in fake_ufw.calls


async def test_default_policy_change_that_tightens_nothing_is_not_guarded(
    app: FastAPI,
    client: httpx.AsyncClient,
    owner: OwnerContext,
    helper: HelperClient,
    fake_ufw: UfwRecorder,
) -> None:
    """The sample already denies incoming, so deny → reject blocks nothing new."""
    await reauth(app)
    resp = await client.post(
        f"{FIREWALL}/default",
        json={"direction": "incoming", "policy": "reject"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text

    # Loosening is never a lock-out risk either.
    resp = await client.post(
        f"{FIREWALL}/default",
        json={"direction": "incoming", "policy": "allow"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200, resp.text
