"""Helper actions for the host firewall (ufw).

``ufw`` is root-only, so every read and every change goes through this module.
Two properties matter more than anything else here:

* **No shell, ever.** Rules are assembled as an argv list from individually
  validated fields (action, direction, port, protocol, address, comment) — the
  caller cannot pass a raw ufw command line, so there is no injection surface.
  Ports match a strict pattern, addresses go through :mod:`ipaddress`, and the
  comment charset is restricted.
* **Locale-independent parsing.** ufw translates its output via gettext, and
  ``LC_ALL=C`` alone is not enough — ``LANGUAGE`` wins over it. Every
  invocation therefore forces ``LANGUAGE=C`` as well, otherwise the parser sees
  e.g. ``状态: 激活`` instead of ``Status: active``.

``ufw status verbose`` and ``ufw status numbered`` are separate calls on
purpose: the two words are alternatives in ufw's CLI, not combinable, and only
``numbered`` prints the indices that deletion needs.

Lock-out protection lives in the API layer (it knows the panel's own port); the
helper only reports the SSH ports it can discover so the API can build the
protected set.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ownpanel.helper.registry import ActionError, register
from ownpanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_INVALID_ARGS,
    HELPER_NOT_FOUND,
    HELPER_TIMEOUT,
    HELPER_UNSUPPORTED,
)

_UFW = "/usr/sbin/ufw"
_SSHD = "/usr/sbin/sshd"

#: Forced C locale. LANGUAGE must be set too — gettext gives it priority over
#: LC_ALL, so without it ufw still answers in the system language.
_ENV = {
    "LANGUAGE": "C",
    "LC_ALL": "C",
    "LANG": "C",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
}

_RUN_TIMEOUT_S = 30.0
_COMMENT_MAX = 120
#: A single port or an inclusive range (ufw's ``6000:6010`` form).
_PORT_RE = re.compile(r"^\d{1,5}(:\d{1,5})?$")
#: Deliberately narrow: no quotes, no shell metacharacters, no newlines.
_COMMENT_RE = re.compile(r"^[\w \-.,:/()一-鿿]*$")

#: ``[ 3] 22/tcp   ALLOW IN   Anywhere   # comment``
_RULE_RE = re.compile(
    r"^\[\s*(?P<num>\d+)\]\s+"
    r"(?P<to>.+?)\s{2,}"
    r"(?P<action>(?:ALLOW|DENY|REJECT|LIMIT)(?:\s+(?:IN|OUT|FWD))?)\s{2,}"
    r"(?P<from>.+?)"
    r"(?:\s+#\s*(?P<comment>.*?))?\s*$"
)
_STATUS_RE = re.compile(r"^Status:\s*(\w+)", re.MULTILINE)
_LOGGING_RE = re.compile(r"^Logging:\s*(.+)$", re.MULTILINE)
_DEFAULT_RE = re.compile(r"^Default:\s*(.+)$", re.MULTILINE)
_DEFAULT_ENTRY_RE = re.compile(r"(\w+)\s*\((\w+)\)")
_SSHD_PORT_RE = re.compile(r"^port\s+(\d{1,5})$", re.MULTILINE)

Policy = Literal["allow", "deny", "reject"]
RuleAction = Literal["allow", "deny", "reject", "limit"]
Direction = Literal["in", "out"]


# ---------------------------------------------------------------------------
# Argument models
# ---------------------------------------------------------------------------


class EmptyArgs(BaseModel):
    pass


class AddRuleArgs(BaseModel):
    action: RuleAction
    direction: Direction = "in"
    #: ``"22"`` or ``"6000:6010"``; omit to match every port.
    port: str = ""
    proto: Literal["", "tcp", "udp"] = ""
    #: ``"any"``/empty or an IP / CIDR.
    from_addr: str = "any"
    to_addr: str = "any"
    comment: str = Field(default="", max_length=_COMMENT_MAX)

    @field_validator("port")
    @classmethod
    def _check_port(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not _PORT_RE.match(value):
            raise ValueError("端口需为 1-65535 或 起始:结束 区间")
        for part in value.split(":"):
            if not 1 <= int(part) <= 65535:
                raise ValueError("端口需在 1-65535 之间")
        bounds = value.split(":")
        if len(bounds) == 2 and int(bounds[0]) >= int(bounds[1]):
            raise ValueError("端口区间的起始值需小于结束值")
        return value

    @field_validator("from_addr", "to_addr")
    @classmethod
    def _check_addr(cls, value: str) -> str:
        value = value.strip()
        if not value or value.lower() == "any":
            return "any"
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError(f"来源/目标需为 IP 或 CIDR: {value}") from exc
        return value

    @field_validator("comment")
    @classmethod
    def _check_comment(cls, value: str) -> str:
        value = value.strip()
        if value and not _COMMENT_RE.match(value):
            raise ValueError("备注含不允许的字符")
        return value


class DeleteRuleArgs(BaseModel):
    #: 1-based index as shown by ``ufw status numbered``.
    num: int = Field(ge=1, le=10000)
    #: What the caller believes sits at ``num`` — guards against index drift.
    expect: str = Field(min_length=1, max_length=512)


class SetDefaultArgs(BaseModel):
    direction: Literal["incoming", "outgoing", "routed"]
    policy: Policy


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, timeout_s: float = _RUN_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run one command by absolute path with an argv array and a forced C locale."""
    try:
        return subprocess.run(  # fixed binary, argv array, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=_ENV,
        )
    except FileNotFoundError as exc:
        raise ActionError(HELPER_UNSUPPORTED, f"{argv[0]} 未安装") from exc
    except subprocess.TimeoutExpired as exc:
        raise ActionError(HELPER_TIMEOUT, f"{argv[0]} 执行超时") from exc


def _ufw(*args: str, timeout_s: float = _RUN_TIMEOUT_S) -> str:
    """Run ufw and return stdout, mapping a non-zero exit to an ActionError."""
    if not os.path.exists(_UFW):
        raise ActionError(HELPER_UNSUPPORTED, "本机未安装 ufw")
    cp = _run([_UFW, *args], timeout_s=timeout_s)
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip().splitlines()
        message = detail[0] if detail else f"ufw 退出码 {cp.returncode}"
        raise ActionError(HELPER_INVALID_ARGS, message)
    return cp.stdout


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_rules(numbered_output: str) -> list[dict[str, Any]]:
    """Parse ``ufw status numbered`` into rule dicts.

    Handles app-profile rules whose ``To`` column is a bare name with spaces
    (``Nginx Full``), the ``(v6)`` marker on either column, and trailing
    ``# comment`` text. Unparseable lines are skipped rather than guessed at.
    """
    rules: list[dict[str, Any]] = []
    for line in numbered_output.splitlines():
        match = _RULE_RE.match(line)
        if match is None:
            continue
        to = match.group("to").strip()
        from_ = match.group("from").strip()
        rules.append(
            {
                "num": int(match.group("num")),
                "to": to,
                "action": " ".join(match.group("action").split()),
                "from": from_,
                "comment": (match.group("comment") or "").strip(),
                "v6": to.endswith("(v6)") or from_.endswith("(v6)"),
            }
        )
    return rules


def parse_verbose(verbose_output: str) -> dict[str, Any]:
    """Pull status/logging/default policies out of ``ufw status verbose``."""
    status = _STATUS_RE.search(verbose_output)
    logging_match = _LOGGING_RE.search(verbose_output)
    defaults: dict[str, str] = {}
    default_match = _DEFAULT_RE.search(verbose_output)
    if default_match:
        # "deny (incoming), allow (outgoing), deny (routed)"
        for policy, direction in _DEFAULT_ENTRY_RE.findall(default_match.group(1)):
            defaults[direction] = policy
    return {
        "active": bool(status and status.group(1).lower() == "active"),
        "logging": logging_match.group(1).strip() if logging_match else "",
        "default_incoming": defaults.get("incoming", ""),
        "default_outgoing": defaults.get("outgoing", ""),
        "default_routed": defaults.get("routed", ""),
    }


def rule_fingerprint(rule: dict[str, Any]) -> str:
    """Stable identity of a rule, independent of its (shifting) index."""
    return f"{rule['to']}|{rule['action']}|{rule['from']}"


def _ssh_ports() -> list[int]:
    """Effective sshd listen ports, via ``sshd -T`` (empty when unavailable).

    ``sshd -T`` resolves Include directives and drop-ins, so it beats parsing
    sshd_config by hand. A host without sshd simply reports no ports.
    """
    if not os.path.exists(_SSHD):
        return []
    cp = _run([_SSHD, "-T"])
    if cp.returncode != 0:
        return []
    ports = {int(p) for p in _SSHD_PORT_RE.findall(cp.stdout) if 0 < int(p) < 65536}
    return sorted(ports)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@register("ufw.status", EmptyArgs)
def ufw_status(args: EmptyArgs) -> dict[str, Any]:
    """Full firewall snapshot: state, default policies, numbered rules, SSH ports.

    A host without ufw reports ``installed=false`` rather than failing, so the
    UI can offer an explanation instead of an error.
    """
    if not os.path.exists(_UFW):
        return {
            "installed": False,
            "active": False,
            "logging": "",
            "default_incoming": "",
            "default_outgoing": "",
            "default_routed": "",
            "rules": [],
            "ssh_ports": _ssh_ports(),
        }
    summary = parse_verbose(_ufw("status", "verbose"))
    rules = parse_rules(_ufw("status", "numbered")) if summary["active"] else []
    return {
        "installed": True,
        **summary,
        "rules": rules,
        "ssh_ports": _ssh_ports(),
    }


@register("ufw.enable", EmptyArgs)
def ufw_enable(args: EmptyArgs) -> dict[str, Any]:
    """Enable the firewall (``--force`` skips ufw's interactive SSH warning)."""
    _ufw("--force", "enable")
    return {"ok": True}


@register("ufw.disable", EmptyArgs)
def ufw_disable(args: EmptyArgs) -> dict[str, Any]:
    _ufw("disable")
    return {"ok": True}


@register("ufw.reload", EmptyArgs)
def ufw_reload(args: EmptyArgs) -> dict[str, Any]:
    _ufw("reload")
    return {"ok": True}


@register("ufw.set_default", SetDefaultArgs)
def ufw_set_default(args: SetDefaultArgs) -> dict[str, Any]:
    _ufw("default", args.policy, args.direction)
    return {"ok": True}


@register("ufw.add_rule", AddRuleArgs)
def ufw_add_rule(args: AddRuleArgs) -> dict[str, Any]:
    """Add one rule, built as an argv array from validated fields.

    ``--force`` is deliberately NOT passed here: on a rule-adding invocation ufw
    silently declines to add anything, so it would turn every call into a no-op.
    """
    if not args.port and args.from_addr == "any":
        raise ActionError(
            HELPER_INVALID_ARGS,
            "必须指定端口或来源地址, 否则该规则会放行/阻断全部流量",
        )
    argv = [args.action, args.direction, "from", args.from_addr, "to", args.to_addr]
    if args.port:
        argv += ["port", args.port]
    if args.proto:
        argv += ["proto", args.proto]
    if args.comment:
        argv += ["comment", args.comment]
    output = _ufw(*argv)
    return {"ok": True, "output": output.strip()}


@register("ufw.delete_rule", DeleteRuleArgs)
def ufw_delete_rule(args: DeleteRuleArgs) -> dict[str, Any]:
    """Delete rule ``num``, but only if it still matches ``expect``.

    ufw renumbers on every change, so an index the UI captured a moment ago can
    now point at a different rule. Re-reading and comparing the fingerprint
    turns a silent wrong-rule deletion into a conflict the user can retry.
    """
    rules = {r["num"]: r for r in parse_rules(_ufw("status", "numbered"))}
    current = rules.get(args.num)
    if current is None:
        raise ActionError(HELPER_NOT_FOUND, f"规则 {args.num} 不存在")
    if rule_fingerprint(current) != args.expect:
        raise ActionError(
            HELPER_CONFLICT,
            f"规则 {args.num} 已经变化(当前为 {rule_fingerprint(current)}),请刷新后重试",
        )
    _ufw("--force", "delete", str(args.num))
    return {"ok": True, "deleted": current}
