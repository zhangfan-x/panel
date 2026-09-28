"""Firewall (ufw) API, mounted under the website module.

Everything ufw-related is root-only, so this router is a thin, audited shell
over the helper's ``ufw.*`` actions plus one piece of logic the helper cannot
do on its own: **lock-out protection**.

The helper knows the host's SSH ports; only the API knows which port the panel
itself listens on. Together they form the *protected* set. Before enabling the
firewall or deleting a rule, the resulting rule set is checked against that
set, and any operation that would leave a protected port uncovered is refused
with ``409`` until the caller repeats it with ``confirm_lockout=true`` — which
additionally requires a recent re-authentication. Losing SSH and the panel at
once on a remote host is unrecoverable without console access, so this is the
one place where an extra click is worth the friction.

Coverage is judged conservatively: a rule only counts as protecting a port when
its ports can actually be read off the ``To`` column. App-profile rules
(``Nginx Full``) report no ports in ``ufw status numbered``, so they never
count as cover — the guard warns rather than assuming safety.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from ownpanel.audit import record_audit
from ownpanel.auth.deps import (
    ActiveSessionDep,
    CurrentAuth,
    DbDep,
    SettingsDep,
    require_csrf,
    require_recent_reauth,
)
from ownpanel.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    HelperActionError,
    NotFoundError,
)
from ownpanel.ipc.client import HelperClient
from ownpanel.ipc.messages import HELPER_CONFLICT, HELPER_INVALID_ARGS, HELPER_NOT_FOUND

firewall_router = APIRouter(prefix="/website/firewall", tags=["firewall"])

_READ_TIMEOUT_S = 20.0
_ACTION_TIMEOUT_S = 40.0

#: ``22/tcp``, ``80,443/tcp``, ``6000:6010/udp`` or a bare ``22``. An app
#: profile name (``Nginx Full``) matches nothing and yields "ports unknown".
_PORT_SPEC_RE = re.compile(r"^([\d,:]+)(?:/(tcp|udp))?(?:\s*\(v6\))?$")

RuleAction = Literal["allow", "deny", "reject", "limit"]
Direction = Literal["in", "out"]
Policy = Literal["allow", "deny", "reject"]


class AddRuleRequest(BaseModel):
    action: RuleAction = "allow"
    direction: Direction = "in"
    port: str = Field(default="", max_length=16)
    proto: Literal["", "tcp", "udp"] = ""
    from_addr: str = Field(default="any", max_length=64)
    to_addr: str = Field(default="any", max_length=64)
    comment: str = Field(default="", max_length=120)


class DeleteRuleRequest(BaseModel):
    #: Fingerprint of the rule the UI displayed, re-checked helper-side.
    expect: str = Field(min_length=1, max_length=512)
    #: Acknowledges that the deletion may cut off SSH or the panel.
    confirm_lockout: bool = False


class EnableRequest(BaseModel):
    confirm_lockout: bool = False


class SetDefaultRequest(BaseModel):
    direction: Literal["incoming", "outgoing", "routed"]
    policy: Policy
    confirm_lockout: bool = False


def _helper(request: Request) -> HelperClient:
    helper: HelperClient = request.app.state.helper
    return helper


async def _call(
    request: Request, action: str, args: dict[str, Any], *, timeout_s: float = _ACTION_TIMEOUT_S
) -> dict[str, Any]:
    """Run a helper ``ufw.*`` action, mapping helper error codes to HTTP status."""
    try:
        result: dict[str, Any] = await _helper(request).acall(action, args, timeout_s=timeout_s)
    except HelperActionError as exc:
        if exc.helper_code == HELPER_NOT_FOUND:
            raise NotFoundError(exc.detail or "not found") from exc
        if exc.helper_code == HELPER_CONFLICT:
            raise ConflictError(exc.detail or "conflict") from exc
        if exc.helper_code == HELPER_INVALID_ARGS:
            raise BadRequestError(exc.detail or "invalid arguments") from exc
        raise
    return result


async def _audit(
    request: Request,
    db: DbDep,
    auth: CurrentAuth,
    action: str,
    target: str,
    *,
    result: str = "ok",
    error_code: str | None = None,
) -> None:
    await record_audit(
        db,
        resource="firewall",
        action=action,
        result=result,
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        error_code=error_code,
        commit=True,
    )


# ---------------------------------------------------------------------------
# Lock-out analysis
# ---------------------------------------------------------------------------


def rule_ports(to: str) -> set[int] | None:
    """Ports a rule's ``To`` column covers; ``None`` when they cannot be read.

    ``None`` (an app profile such as ``Nginx Full``, or the catch-all
    ``Anywhere``) is deliberately *not* treated as coverage by the callers —
    an unreadable rule must never be mistaken for proof that a port is safe.
    """
    match = _PORT_SPEC_RE.match(to.strip())
    if match is None:
        return None
    ports: set[int] = set()
    for part in match.group(1).split(","):
        if not part:
            continue
        if ":" in part:
            start, _, end = part.partition(":")
            if not (start.isdigit() and end.isdigit()):
                return None
            ports.update(range(int(start), min(int(end), 65535) + 1))
        elif part.isdigit():
            ports.add(int(part))
        else:
            return None
    return ports or None


def covered_ports(rules: list[dict[str, Any]]) -> set[int]:
    """Ports definitively allowed inbound over IPv4 by ``rules``.

    IPv6-only rules are skipped on purpose. ufw lists a ``22/tcp (v6)`` twin
    beside most IPv4 rules, and counting it would mean deleting the IPv4 rule
    for SSH looks harmless while actually cutting off every IPv4 client — the
    exact lock-out this guard exists to prevent.
    """
    covered: set[int] = set()
    for rule in rules:
        action = str(rule.get("action", "")).upper()
        if not action.startswith("ALLOW") or action.endswith("OUT"):
            continue
        if rule.get("v6"):
            continue
        ports = rule_ports(str(rule.get("to", "")))
        if ports:
            covered |= ports
    return covered


def protected_ports(settings_port: int, ssh_ports: list[int]) -> list[int]:
    """Ports whose loss would cut off remote administration."""
    return sorted({settings_port, *ssh_ports})


def endangered(rules: list[dict[str, Any]], protected: list[int]) -> list[int]:
    """Protected ports left without an inbound allow rule in ``rules``."""
    covered = covered_ports(rules)
    return [port for port in protected if port not in covered]


async def _snapshot(request: Request, settings_port: int) -> dict[str, Any]:
    """Helper status enriched with the protected-port analysis."""
    status = await _call(request, "ufw.status", {}, timeout_s=_READ_TIMEOUT_S)
    ssh_ports = [int(p) for p in status.get("ssh_ports", []) if isinstance(p, int)]
    protected = protected_ports(settings_port, ssh_ports)
    rules = list(status.get("rules", []))
    return {
        **status,
        "panel_port": settings_port,
        "protected_ports": protected,
        "uncovered_ports": endangered(rules, protected),
    }


def newly_endangered(
    before: list[dict[str, Any]], after: list[dict[str, Any]], protected: list[int]
) -> list[int]:
    """Protected ports that ``after`` strands but ``before`` did not.

    Only the *delta* may block an operation. A port that already had no rule
    (the panel's own port often does not, when the panel is reached through a
    reverse proxy) is not this operation's doing, and holding every unrelated
    rule deletion hostage to it would train the user to click through the
    warning — which is precisely how the guard stops working.
    """
    was = set(endangered(before, protected))
    return [port for port in endangered(after, protected) if port not in was]


def _guard(uncovered: list[int], confirmed: bool, what: str) -> None:
    """Refuse a lock-out-capable operation unless it was explicitly confirmed."""
    if not uncovered or confirmed:
        return
    ports = ", ".join(str(p) for p in uncovered)
    raise ConflictError(
        f"{what}后 SSH/面板端口 {ports} 将没有放行规则, 可能导致无法再连接本机; "
        f"请先添加放行规则, 或确认知晓风险后重试"
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@firewall_router.get("/status")
async def firewall_status(
    request: Request, settings: SettingsDep, _auth: ActiveSessionDep
) -> dict[str, Any]:
    """Firewall state, default policies, numbered rules and the protected-port view.

    ``installed=false`` (no ufw on the host) is a normal answer, not an error.
    """
    return await _snapshot(request, settings.server.port)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@firewall_router.post(
    "/enable",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def firewall_enable(
    body: EnableRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Turn the firewall on (CSRF + recent re-auth).

    With the usual ``deny (incoming)`` default, enabling drops every port that
    has no allow rule — including, potentially, the one this request arrived on.
    """
    try:
        snapshot = await _snapshot(request, settings.server.port)
        if not snapshot.get("installed"):
            raise BadRequestError("本机未安装 ufw")
        _guard(snapshot["uncovered_ports"], body.confirm_lockout, "启用防火墙")
        await _call(request, "ufw.enable", {})
    except AppError as exc:
        await _audit(request, db, auth, "firewall.enable", "", result="denied", error_code=exc.code)
        raise
    await _audit(request, db, auth, "firewall.enable", "")
    return await _snapshot(request, settings.server.port)


@firewall_router.post(
    "/disable",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def firewall_disable(
    request: Request, settings: SettingsDep, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Turn the firewall off (CSRF + recent re-auth).

    No lock-out guard: disabling opens ports, it never closes them.
    """
    try:
        await _call(request, "ufw.disable", {})
    except AppError as exc:
        await _audit(
            request, db, auth, "firewall.disable", "", result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, "firewall.disable", "")
    return await _snapshot(request, settings.server.port)


@firewall_router.post("/rules", dependencies=[Depends(require_csrf)])
async def firewall_add_rule(
    body: AddRuleRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Add one rule (CSRF). Adding can only widen access, so no lock-out guard."""
    target = f"{body.action} {body.direction} {body.port or 'any'}/{body.proto or 'any'}"
    try:
        await _call(
            request,
            "ufw.add_rule",
            {
                "action": body.action,
                "direction": body.direction,
                "port": body.port,
                "proto": body.proto,
                "from_addr": body.from_addr,
                "to_addr": body.to_addr,
                "comment": body.comment,
            },
        )
    except AppError as exc:
        await _audit(
            request, db, auth, "firewall.rule_add", target, result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, "firewall.rule_add", target)
    return await _snapshot(request, settings.server.port)


@firewall_router.delete(
    "/rules/{num}",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def firewall_delete_rule(
    num: Annotated[int, Path(ge=1, le=10000)],
    body: DeleteRuleRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Delete rule ``num`` (CSRF + recent re-auth).

    Guarded twice over: the API refuses when removing this rule would leave a
    protected port uncovered (unless confirmed), and the helper re-checks that
    ``num`` still points at the rule the UI showed — ufw renumbers on every
    change, so a stale index would otherwise delete the wrong rule silently.
    """
    try:
        snapshot = await _snapshot(request, settings.server.port)
        remaining = [r for r in snapshot.get("rules", []) if int(r.get("num", 0)) != num]
        uncovered = newly_endangered(
            list(snapshot.get("rules", [])), remaining, snapshot["protected_ports"]
        )
        _guard(uncovered, body.confirm_lockout, "删除该规则")
        await _call(request, "ufw.delete_rule", {"num": num, "expect": body.expect})
    except AppError as exc:
        await _audit(
            request,
            db,
            auth,
            "firewall.rule_delete",
            str(num),
            result="denied",
            error_code=exc.code,
        )
        raise
    await _audit(request, db, auth, "firewall.rule_delete", str(num))
    return await _snapshot(request, settings.server.port)


@firewall_router.post(
    "/default",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def firewall_set_default(
    body: SetDefaultRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Change a default policy (CSRF + recent re-auth).

    Tightening the *incoming* default is the other way to lock yourself out, so
    it takes the same guard as enabling — but only when it actually tightens
    anything: going from ``deny`` to ``reject`` blocks nothing that was not
    already blocked.
    """
    target = f"{body.direction}={body.policy}"
    try:
        if body.direction == "incoming" and body.policy != "allow":
            snapshot = await _snapshot(request, settings.server.port)
            if snapshot.get("default_incoming") == "allow":
                _guard(snapshot["uncovered_ports"], body.confirm_lockout, "收紧入站默认策略")
        await _call(
            request, "ufw.set_default", {"direction": body.direction, "policy": body.policy}
        )
    except AppError as exc:
        await _audit(
            request, db, auth, "firewall.set_default", target, result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, "firewall.set_default", target)
    return await _snapshot(request, settings.server.port)


@firewall_router.post("/reload", dependencies=[Depends(require_csrf)])
async def firewall_reload(
    request: Request, settings: SettingsDep, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Reload ufw so on-disk rule changes take effect (CSRF)."""
    try:
        await _call(request, "ufw.reload", {})
    except AppError as exc:
        await _audit(request, db, auth, "firewall.reload", "", result="denied", error_code=exc.code)
        raise
    await _audit(request, db, auth, "firewall.reload", "")
    return await _snapshot(request, settings.server.port)
