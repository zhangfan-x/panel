"""Panel-managed custom Clash rules, persisted outside the Clash config file.

Custom rules live in ``data_dir/proxy_custom_rules.json`` so they survive
config-file uploads: every config write re-injects them as a marker-delimited
block at the head of the top-level ``rules:`` list (highest match priority).
Injection is plain text surgery — the user's YAML formatting and comments are
preserved — with a full YAML validation afterwards as the safety net.

Rule strings follow Clash's ``TYPE,ARGUMENT,POLICY`` shape (``MATCH,POLICY``
for the catch-all). Validation here is deliberately loose — the controller is
the final authority at reload time — but rejects characters that would break
the single-quoted YAML injection.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mypanel.config import Settings
from mypanel.errors import BadRequestError

RULES_FILE = "proxy_custom_rules.json"

BEGIN_MARK = "# BEGIN mypanel custom rules (auto-managed, do not edit)"
END_MARK = "# END mypanel custom rules"

_RULE_MAX_LEN = 512
#: First comma-separated segment: a Clash rule type (DOMAIN-SUFFIX, GEOIP, MATCH…).
_TYPE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")
#: Characters that would break the single-quoted YAML scalar or the line format.
_FORBIDDEN_CHARS = ("'", '"', "\n", "\r", "#", "(", ")")

#: Top-level ``rules:`` key opening a block sequence (rest of line empty/comment).
_RULES_BLOCK_LINE = re.compile(r"^rules:\s*(#.*)?$", re.MULTILINE)
#: Top-level ``rules:`` key with inline content (flow sequence, anchor, …).
_RULES_INLINE_LINE = re.compile(r"^rules:\s*\S", re.MULTILINE)


def rules_path(settings: Settings) -> Path:
    return settings.paths.data_dir / RULES_FILE


def load_rules(settings: Settings) -> list[dict[str, Any]]:
    """Stored custom rules ``[{id, rule, created_at}, ...]`` (empty when absent)."""
    path = rules_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("rule"), str) and item.get("id"):
            out.append(
                {
                    "id": str(item["id"]),
                    "rule": item["rule"],
                    "created_at": str(item.get("created_at", "")),
                }
            )
    return out


def save_rules(settings: Settings, rules: list[dict[str, Any]]) -> None:
    """Atomically persist the custom-rule list (data_dir is created by startup)."""
    settings.paths.data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    atomic_write_text(rules_path(settings), json.dumps(rules, ensure_ascii=False, indent=2))


def new_rule_entry(rule: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "rule": rule,
        "created_at": datetime.now(tz=UTC).isoformat(),
    }


def validate_rule(rule: str) -> str:
    """Normalise and validate one rule string; returns the cleaned form.

    Raises:
        BadRequestError: empty/too long, forbidden characters, malformed
            segments, or an unrecognisable rule type.
    """
    cleaned = rule.strip()
    if not cleaned:
        raise BadRequestError("规则不能为空")
    if len(cleaned) > _RULE_MAX_LEN:
        raise BadRequestError(f"规则过长(最多 {_RULE_MAX_LEN} 字符)")
    for ch in _FORBIDDEN_CHARS:
        if ch in cleaned:
            raise BadRequestError(f"规则包含不允许的字符: {ch!r}")
    segments = [seg.strip() for seg in cleaned.split(",")]
    if any(not seg for seg in segments):
        raise BadRequestError("规则包含空段(连续逗号)")
    rtype = segments[0].upper()
    if not _TYPE_PATTERN.match(rtype):
        raise BadRequestError(f"无法识别的规则类型: {segments[0]}")
    if rtype == "MATCH":
        if len(segments) != 2:
            raise BadRequestError("MATCH 规则格式应为 MATCH,策略")
    elif len(segments) < 3 or len(segments) > 4:
        raise BadRequestError("规则格式应为 类型,匹配内容,策略[,no-resolve]")
    segments[0] = rtype
    return ",".join(segments)


def strip_injected(content: str) -> str:
    """Remove any previously injected marker block (and stray marker lines)."""
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == BEGIN_MARK:
            inside = True
            continue
        if stripped == END_MARK:
            inside = False
            continue
        if not inside:
            out.append(line)
    return "".join(out)


def inject_rules(content: str, rules: list[str]) -> str:
    """Insert ``rules`` as a marker block at the head of the ``rules:`` list.

    Any existing injected block is removed first, so the operation is
    idempotent. With no custom rules the result is simply the stripped text.

    Raises:
        BadRequestError: the config declares ``rules:`` in inline/flow style,
            which plain-text injection cannot extend.
    """
    base = strip_injected(content)
    if not rules:
        return base
    block_lines = [f"  {BEGIN_MARK}"]
    block_lines += [f"  - '{rule}'" for rule in rules]
    block_lines.append(f"  {END_MARK}")
    block = "\n".join(block_lines)
    match = _RULES_BLOCK_LINE.search(base)
    if match is None:
        if _RULES_INLINE_LINE.search(base):
            raise BadRequestError(
                "配置文件的 rules 使用了内联(flow)写法, 无法注入自定义规则; "
                "请将 rules 改为逐行列表格式"
            )
        suffix = "" if (not base or base.endswith("\n")) else "\n"
        return f"{base}{suffix}rules:\n{block}\n"
    insert_at = match.end()
    return f"{base[:insert_at]}\n{block}{base[insert_at:]}"


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file in the same dir + rename)."""
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    except OSError as exc:
        raise BadRequestError(f"failed to write {path.name}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            with contextlib.suppress(OSError):
                os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise BadRequestError(f"failed to write {path.name}: {exc}") from exc
