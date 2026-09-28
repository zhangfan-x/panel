"""Helper systemd actions: unit list/status, lifecycle verbs and journal access.

All operations shell out to ``/usr/bin/systemctl`` / ``/usr/bin/journalctl``
with fixed absolute paths, argument arrays and ``shell=False`` (docs/
code-plan-m2-m5.md §0.1, §3.2). Unit names pass a strict allowlist regex before
any subprocess runs, and every positional unit argument is preceded by ``--``
(journalctl gets it as the value of ``-u``) so a name can never be parsed as an
option. Handlers self-register via ``@register`` at import time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ownpanel.helper.registry import ActionError, ActionOutcome, register
from ownpanel.ipc.messages import (
    HELPER_DENIED,
    HELPER_INTERNAL,
    HELPER_INVALID_ARGS,
    HELPER_NOT_FOUND,
    HELPER_TIMEOUT,
    HELPER_UNSUPPORTED,
)

_SYSTEMCTL = "/usr/bin/systemctl"
_JOURNALCTL = "/usr/bin/journalctl"

_READ_TIMEOUT_S = 10.0
_VERB_TIMEOUT_S = 30.0

# docs/code-plan-m2-m5.md §3.2: allowlist of unit-name characters + known unit
# suffixes. Everything else (slashes, whitespace, shell metacharacters, NUL...)
# is rejected before any subprocess is spawned.
_UNIT_RE = re.compile(
    r"[A-Za-z0-9:._@-]+\."
    r"(service|socket|timer|target|path|mount|slice|scope|device|swap|automount)"
)
_UNIT_NAME_MAX = 256

_VERBS = ("start", "stop", "restart", "reload", "enable", "disable")

_STATUS_PROPS = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "Description",
    "MainPID",
    "MemoryCurrent",
    "ActiveEnterTimestamp",
    "FragmentPath",
)

# Keep parsed journal snapshots comfortably below MAX_FRAME_BYTES (256 KiB).
_JOURNAL_BUDGET_BYTES = 200 * 1024
_JOURNAL_MESSAGE_MAX = 4000
_ERROR_DETAIL_MAX = 500

# Live ``journalctl -f`` children, keyed by pid. Keeping the Popen referenced
# prevents GC from finalising it behind our back; entries are reaped
# opportunistically on the next journal call. The child exits on its own once
# every read end of its stdout pipe is closed (EPIPE/SIGPIPE).
_FOLLOWERS: dict[int, subprocess.Popen[bytes]] = {}


class EmptyArgs(BaseModel):
    pass


class UnitArgs(BaseModel):
    unit: str


class JournalArgs(BaseModel):
    unit: str
    lines: int = Field(default=200, ge=1, le=1000)
    follow: bool = False


def _validate_unit(name: str) -> str:
    """Require a plain unit name matching the allowlist regex; returns it unchanged.

    Raises:
        ActionError: ``helper.invalid_args`` for anything not matching (paths,
            ``..``, whitespace, shell metacharacters, wrong suffix, overlong).
    """
    if len(name) > _UNIT_NAME_MAX or ".." in name or not _UNIT_RE.fullmatch(name):
        raise ActionError(HELPER_INVALID_ARGS, f"invalid unit name: {name!r}")
    return name


def _run(argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run one command (absolute path, arg array, no shell) and capture output.

    Raises:
        ActionError: ``helper.timeout`` when the deadline passes,
            ``helper.unsupported`` when the binary cannot be executed.
    """
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionError(HELPER_TIMEOUT, f"command timed out: {argv[0]}") from exc
    except OSError as exc:
        raise ActionError(HELPER_UNSUPPORTED, f"cannot run {argv[0]}: {exc}") from exc


def _failure(unit: str, cp: subprocess.CompletedProcess[str]) -> ActionError:
    """Map a non-zero systemctl/journalctl exit to a stable helper error code."""
    text = cp.stderr.strip() or cp.stdout.strip() or f"exit status {cp.returncode}"
    detail = " ".join(text.split())[:_ERROR_DETAIL_MAX]
    lowered = detail.lower()
    if "not found" in lowered or "not loaded" in lowered:
        return ActionError(HELPER_NOT_FOUND, f"{unit}: {detail}")
    if (
        "masked" in lowered
        or "access denied" in lowered
        or "permission denied" in lowered
        or "authentication required" in lowered
    ):
        return ActionError(HELPER_DENIED, f"{unit}: {detail}")
    if "not applicable" in lowered or "not supported" in lowered:
        return ActionError(HELPER_UNSUPPORTED, f"{unit}: {detail}")
    return ActionError(HELPER_INTERNAL, f"{unit}: {detail}")


def _parse_json_array(text: str, *, source: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ActionError(HELPER_INTERNAL, f"unparseable {source} output") from exc
    if not isinstance(data, list):
        raise ActionError(HELPER_INTERNAL, f"unexpected {source} output shape")
    return [row for row in data if isinstance(row, dict)]


@register("systemd.list_units", EmptyArgs)
def systemd_list_units(args: EmptyArgs) -> dict[str, Any]:
    """Merged view of loaded service units and installed service unit files.

    ``list-units --all`` provides load/active/sub/description for units in
    memory; ``list-unit-files`` contributes the enabled state and any installed
    services that are not currently loaded (those get an inactive placeholder).
    """
    units_cp = _run(
        [_SYSTEMCTL, "list-units", "--type=service", "--all", "--no-pager", "-o", "json"],
        timeout_s=_READ_TIMEOUT_S,
    )
    if units_cp.returncode != 0:
        raise _failure("list-units", units_cp)
    files_cp = _run(
        [_SYSTEMCTL, "list-unit-files", "--type=service", "--no-pager", "-o", "json"],
        timeout_s=_READ_TIMEOUT_S,
    )
    if files_cp.returncode != 0:
        raise _failure("list-unit-files", files_cp)

    enabled_by_name: dict[str, str] = {}
    for row in _parse_json_array(files_cp.stdout, source="list-unit-files"):
        name = row.get("unit_file")
        if isinstance(name, str):
            enabled_by_name[name] = str(row.get("state") or "")

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _parse_json_array(units_cp.stdout, source="list-units"):
        name = row.get("unit")
        if not isinstance(name, str):
            continue
        seen.add(name)
        units.append(
            {
                "name": name,
                "load": str(row.get("load") or ""),
                "active": str(row.get("active") or ""),
                "sub": str(row.get("sub") or ""),
                "description": str(row.get("description") or ""),
                "enabled": enabled_by_name.get(name, ""),
            }
        )
    for name, state in enabled_by_name.items():
        if name not in seen:
            units.append(
                {
                    "name": name,
                    "load": "",
                    "active": "inactive",
                    "sub": "dead",
                    "description": "",
                    "enabled": state,
                }
            )
    units.sort(key=lambda unit: str(unit["name"]))
    return {"units": units}


def _show_properties(unit: str) -> dict[str, str]:
    cp = _run(
        [_SYSTEMCTL, "show", "-p", ",".join(_STATUS_PROPS), "--no-pager", "--", unit],
        timeout_s=_READ_TIMEOUT_S,
    )
    if cp.returncode != 0:
        raise _failure(unit, cp)
    props: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return props


def _unit_exists(unit: str) -> bool:
    """Cross-check that systemd knows the unit (loaded or installed on disk)."""
    cp = _run(
        [_SYSTEMCTL, "show", "-p", "LoadState", "--no-pager", "--", unit],
        timeout_s=_READ_TIMEOUT_S,
    )
    if cp.returncode != 0:
        return False
    value = cp.stdout.strip().partition("=")[2]
    return value not in ("", "not-found")


@register("systemd.status", UnitArgs)
def systemd_status(args: UnitArgs) -> dict[str, Any]:
    """Detailed status of one unit via ``systemctl show -p ...``."""
    unit = _validate_unit(args.unit)
    props = _show_properties(unit)
    if props.get("LoadState", "not-found") == "not-found":
        raise ActionError(HELPER_NOT_FOUND, f"no such unit: {unit}")
    main_pid_raw = props.get("MainPID", "")
    memory_raw = props.get("MemoryCurrent", "")
    # MemoryCurrent is "[not set]" (or u64 max on older systemd) when unknown.
    memory = int(memory_raw) if memory_raw.isdigit() and int(memory_raw) < 2**63 else None
    return {
        "name": props.get("Id") or unit,
        "active": props.get("ActiveState", ""),
        "sub": props.get("SubState", ""),
        "load": props.get("LoadState", ""),
        "enabled": props.get("UnitFileState", ""),
        "description": props.get("Description", ""),
        "main_pid": int(main_pid_raw) if main_pid_raw.isdigit() else 0,
        "memory": memory,
        "since": props.get("ActiveEnterTimestamp", ""),
        "fragment_path": props.get("FragmentPath", ""),
    }


def _verb_handler(verb: str) -> Callable[[UnitArgs], dict[str, Any]]:
    """Build the handler for one lifecycle verb (start/stop/.../disable)."""

    def handler(args: UnitArgs) -> dict[str, Any]:
        unit = _validate_unit(args.unit)
        if not _unit_exists(unit):
            raise ActionError(HELPER_NOT_FOUND, f"no such unit: {unit}")
        cp = _run([_SYSTEMCTL, verb, "--", unit], timeout_s=_VERB_TIMEOUT_S)
        if cp.returncode != 0:
            raise _failure(unit, cp)
        return {"ok": True}

    handler.__name__ = f"systemd_{verb}"
    handler.__doc__ = f"Run ``systemctl {verb}`` on one existing, validated unit."
    return handler


for _verb in _VERBS:
    register(f"systemd.{_verb}", UnitArgs)(_verb_handler(_verb))


def _journal_entry(record: dict[str, Any], fallback_unit: str) -> dict[str, Any]:
    """Reduce one journalctl JSON record to the wire shape {ts,message,priority,unit}."""
    message = record.get("MESSAGE")
    if isinstance(message, list):
        # journalctl encodes non-UTF-8 payloads as arrays of byte values.
        try:
            message = bytes(int(byte) & 0xFF for byte in message).decode("utf-8", "replace")
        except (TypeError, ValueError):
            message = ""
    elif not isinstance(message, str):
        message = "" if message is None else str(message)
    ts_raw = record.get("__REALTIME_TIMESTAMP")
    ts = ""
    if isinstance(ts_raw, str) and ts_raw.isdigit():
        ts = datetime.fromtimestamp(int(ts_raw) / 1_000_000, tz=UTC).isoformat()
    priority_raw = record.get("PRIORITY")
    priority = 6
    if isinstance(priority_raw, str) and priority_raw.isdigit():
        priority = int(priority_raw)
    unit = record.get("_SYSTEMD_UNIT") or record.get("UNIT") or fallback_unit
    return {
        "ts": ts,
        "message": message[:_JOURNAL_MESSAGE_MAX],
        "priority": priority,
        "unit": str(unit),
    }


def _reap_followers() -> None:
    """Drop (and implicitly wait on) follower children that already exited."""
    for pid, proc in list(_FOLLOWERS.items()):
        if proc.poll() is not None:
            _FOLLOWERS.pop(pid, None)


def _journal_follow(unit: str, lines: int) -> ActionOutcome:
    """Spawn ``journalctl -f`` and hand the pipe read end to the API process.

    The write end is closed here after the fork, so once the API (and the
    server's own copy after sendmsg) closes the read end, the child's next
    write fails and journalctl exits; the zombie is reaped on a later call.
    """
    _reap_followers()
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            [_JOURNALCTL, "-u", unit, "-o", "json", "-n", str(lines), "-f", "--no-pager"],
            stdin=subprocess.DEVNULL,
            stdout=write_fd,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise ActionError(HELPER_UNSUPPORTED, f"cannot run {_JOURNALCTL}: {exc}") from exc
    os.close(write_fd)
    _FOLLOWERS[proc.pid] = proc
    return ActionOutcome(result={"pid": proc.pid}, fds=[read_fd])


@register("systemd.journal", JournalArgs)
def systemd_journal(args: JournalArgs) -> dict[str, Any] | ActionOutcome:
    """Journal snapshot (parsed lines) or live follow (raw journalctl JSON via fd)."""
    unit = _validate_unit(args.unit)
    if args.follow:
        return _journal_follow(unit, args.lines)
    cp = _run(
        [_JOURNALCTL, "-u", unit, "-o", "json", "-n", str(args.lines), "--no-pager"],
        timeout_s=_READ_TIMEOUT_S,
    )
    if cp.returncode != 0:
        raise _failure(unit, cp)
    entries: list[dict[str, Any]] = []
    budget = _JOURNAL_BUDGET_BYTES
    # Walk newest-first so the byte budget trims the OLDEST lines, then restore
    # chronological order. Keeps the response frame under MAX_FRAME_BYTES.
    for raw in reversed(cp.stdout.splitlines()):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        entry = _journal_entry(record, unit)
        cost = len(str(entry["message"])) + 120
        if budget < cost:
            break
        budget -= cost
        entries.append(entry)
    entries.reverse()
    return {"lines": entries}
