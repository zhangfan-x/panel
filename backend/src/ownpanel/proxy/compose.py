"""Config synthesis pipeline: subscription → extend-config → extend-script → YAML.

The panel treats the Clash config file as a *product*, not a source: a
subscription provides the base document, an "extend config" (a merge YAML) layers
the user's local customisations on top, and an optional "extend script"
(JavaScript, run in a QuickJS sandbox) gets the last word. Regenerating the file
from those three inputs is what lets a subscription update refresh nodes without
wiping the user's DNS block, extra proxies or rule tweaks.

The merge semantics mirror Clash Verge Rev's so that scripts and merge files from
that ecosystem work unchanged: ``prepend-*``/``append-*`` splice into the three
top-level lists, a plain ``rules``/``proxies``/``proxy-groups`` replaces one
outright, and every other key deep-merges.

Everything here is a pure function over text/dicts except :func:`run_script`,
which drives the (blocking, C-level) QuickJS interpreter — callers on the event
loop should use :func:`compose_config`, which hands it to a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import yaml

from ownpanel.errors import BadRequestError

#: Top-level lists that support ``prepend-``/``append-`` splicing.
SPLICE_KEYS = ("rules", "proxies", "proxy-groups")

#: Hard cap on ``console.*`` lines kept from a script run (mirrored in JS).
MAX_LOG_LINES = 200
#: Hard cap on one captured log line.
MAX_LOG_CHARS = 2000

_DEFAULT_SCRIPT_TIMEOUT_S = 5.0
_DEFAULT_MEMORY_LIMIT_MB = 64
#: QuickJS stack budget; the default is small enough that deep-but-legitimate
#: recursion over a large config would overflow.
_STACK_SIZE_BYTES = 4 * 1024 * 1024

#: Name of the wrapper function compiled around the user's script.
_ENTRY = "__ownpanel_entry"
#: Shared prefix of every prelude identifier, used to drop wrapper frames.
_PRELUDE_MARK = "__ownpanel_"

#: Prelude + epilogue wrapped around the user script. The prelude installs a
#: capturing ``console`` (QuickJS has none), the epilogue exposes a single
#: JSON-in/JSON-out entry point so no object marshalling crosses the boundary.
_SCRIPT_PRELUDE = """
var __ownpanel_logs = [];
function __ownpanel_fmt(value) {
  if (typeof value === 'string') { return value; }
  try { var s = JSON.stringify(value); return s === undefined ? String(value) : s; }
  catch (e) { return String(value); }
}
function __ownpanel_log() {
  if (__ownpanel_logs.length >= %(max_lines)d) { return; }
  var parts = [];
  for (var i = 0; i < arguments.length; i++) { parts.push(__ownpanel_fmt(arguments[i])); }
  __ownpanel_logs.push(parts.join(' ').slice(0, %(max_chars)d));
}
var console = { log: __ownpanel_log, info: __ownpanel_log,
                warn: __ownpanel_log, error: __ownpanel_log, debug: __ownpanel_log };
"""

_SCRIPT_EPILOGUE = """
function %(entry)s(__ownpanel_json) {
  if (typeof main !== 'function') {
    throw new Error('脚本必须定义一个 main(config) 函数');
  }
  var __ownpanel_out = main(JSON.parse(__ownpanel_json));
  if (__ownpanel_out === null || typeof __ownpanel_out !== 'object'
      || Array.isArray(__ownpanel_out)) {
    throw new Error('main(config) 必须返回一个对象');
  }
  return JSON.stringify({ config: __ownpanel_out, logs: __ownpanel_logs });
}
"""


def parse_yaml_mapping(text: str, *, what: str) -> dict[str, Any]:
    """Parse ``text`` as a YAML mapping; empty text yields ``{}``.

    Raises:
        BadRequestError: invalid YAML, or a document that is not a mapping.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BadRequestError(f"{what} 不是合法的 YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BadRequestError(f"{what} 的顶层必须是键值映射")
    return data


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base`` (mappings merge, rest overwrites)."""
    out = dict(base)
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def apply_merge(config: dict[str, Any], merge: dict[str, Any]) -> dict[str, Any]:
    """Layer an extend-config mapping over ``config`` (Clash Verge Rev semantics).

    ``prepend-rules``/``append-rules`` (likewise for ``proxies`` and
    ``proxy-groups``) splice into the corresponding top-level list; a plain
    ``rules``/``proxies``/``proxy-groups`` in ``merge`` replaces it outright.
    Every other key deep-merges, so ``dns: {enable: true}`` tweaks one field
    instead of dropping the subscription's whole ``dns`` block.
    """
    splice: dict[str, tuple[list[Any], list[Any]]] = {}
    plain: dict[str, Any] = {}
    for key, value in merge.items():
        if key.startswith(("prepend-", "append-")):
            prefix, _, target = key.partition("-")
            if target in SPLICE_KEYS:
                before, after = splice.setdefault(target, ([], []))
                (before if prefix == "prepend" else after).extend(_as_list(value))
                continue
        plain[key] = value

    out = _deep_merge(config, plain)
    for target, (before, after) in splice.items():
        out[target] = [*before, *_as_list(out.get(target)), *after]
    return out


#: ``<input>:N`` frames in a QuickJS traceback, for remapping to user line numbers.
_TRACE_LINE = re.compile(r"<input>:(\d+)")


def _remap_lines(message: str, offset: int) -> str:
    """Rewrite a QuickJS traceback in terms of the user's own script.

    The compiled source is prelude + user code + epilogue, so QuickJS reports
    lines that are ``offset`` further down than what the user wrote. Frames from
    the panel's own wrapper are dropped — they only add noise.
    """

    def shift(match: re.Match[str]) -> str:
        line = int(match.group(1)) - offset
        return f"第 {line} 行" if line > 0 else "<内部>"

    kept = [
        line for line in message.splitlines() if _ENTRY not in line and _PRELUDE_MARK not in line
    ]
    return _TRACE_LINE.sub(shift, "\n".join(kept)).strip()


def _quickjs_function(code: str) -> tuple[Any, int]:
    """Compile ``code`` into a sandboxed QuickJS entry function.

    Returns the function plus the number of prelude lines prepended to ``code``,
    so tracebacks can be remapped onto the user's own line numbers.

    ``quickjs`` is imported lazily: it is a C extension without a wheel on every
    platform, and only the extend-script feature needs it — a panel that cannot
    build it keeps every other proxy feature working.
    """
    try:
        import quickjs  # type: ignore[import-untyped]  # deliberately lazy; see docstring
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise BadRequestError(
            "扩展脚本功能不可用: 未安装 quickjs 运行时(pip install quickjs)"
        ) from exc

    prelude = _SCRIPT_PRELUDE % {"max_lines": MAX_LOG_LINES, "max_chars": MAX_LOG_CHARS}
    epilogue = _SCRIPT_EPILOGUE % {"entry": _ENTRY}
    source = f"{prelude}\n{code}\n{epilogue}"
    offset = prelude.count("\n") + 1
    try:
        return quickjs.Function(_ENTRY, source), offset
    except Exception as exc:
        raise BadRequestError(f"扩展脚本编译失败: {_remap_lines(str(exc), offset)}") from exc


def run_script(
    config: dict[str, Any],
    script: str,
    *,
    timeout_s: float = _DEFAULT_SCRIPT_TIMEOUT_S,
    memory_limit_mb: int = _DEFAULT_MEMORY_LIMIT_MB,
) -> tuple[dict[str, Any], list[str]]:
    """Run ``main(config)`` from ``script`` in a QuickJS sandbox.

    The sandbox has no filesystem, network or module access — QuickJS's bare
    global object plus a capturing ``console``. The config crosses the boundary
    as JSON in both directions, so only plain data can come back.

    Blocking: QuickJS runs to completion in the calling thread. Returns the new
    config plus the captured ``console`` lines.

    Raises:
        BadRequestError: compile error, runtime exception, memory/time limit hit,
            or a ``main`` that did not return an object.
    """
    fn, offset = _quickjs_function(script)
    fn.set_memory_limit(max(1, memory_limit_mb) * 1024 * 1024)
    fn.set_max_stack_size(_STACK_SIZE_BYTES)
    # The interpreter's own interrupt handler; asyncio.wait_for cannot preempt a
    # blocking C call, so this is what actually stops a runaway script.
    fn.set_time_limit(max(0.1, timeout_s))
    try:
        raw = fn(json.dumps(config, ensure_ascii=False))
    except BaseException as exc:
        # quickjs raises JSException/StackOverflow (and MemoryError on the memory
        # limit); none of them should escape as a 500.
        raise BadRequestError(f"扩展脚本执行失败: {_remap_lines(str(exc), offset)}") from exc

    if not isinstance(raw, str):
        raise BadRequestError("扩展脚本执行失败: 未返回结果")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise BadRequestError(f"扩展脚本返回值无法序列化: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        raise BadRequestError("扩展脚本执行失败: main(config) 必须返回一个对象")
    logs = [str(line) for line in _as_list(payload.get("logs"))][:MAX_LOG_LINES]
    return payload["config"], logs


class _IndentedDumper(yaml.SafeDumper):
    """SafeDumper that indents block sequences under their key.

    PyYAML's default emits ``rules:\\n- MATCH,DIRECT`` (indentless sequences),
    which does not match the ``  - '...'`` block
    :func:`ownpanel.proxy.custom_rules.inject_rules` splices in — mixing the two
    styles in one document is a YAML syntax error. Indenting also matches how
    Clash configs are conventionally written.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow=flow, indentless=False)


def dump_yaml(config: dict[str, Any]) -> str:
    """Serialise the composed config back to YAML (unicode kept, key order kept)."""
    return yaml.dump(
        config,
        Dumper=_IndentedDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def compose_sync(
    base_text: str,
    merge_text: str,
    script_text: str,
    *,
    script_timeout_s: float = _DEFAULT_SCRIPT_TIMEOUT_S,
    memory_limit_mb: int = _DEFAULT_MEMORY_LIMIT_MB,
) -> tuple[str, list[str]]:
    """Run the whole pipeline synchronously; returns (yaml_text, console_lines)."""
    config = parse_yaml_mapping(base_text, what="订阅配置")
    merge = parse_yaml_mapping(merge_text, what="扩展配置")
    config = apply_merge(config, merge)
    logs: list[str] = []
    if script_text.strip():
        config, logs = run_script(
            config,
            script_text,
            timeout_s=script_timeout_s,
            memory_limit_mb=memory_limit_mb,
        )
    return dump_yaml(config), logs


async def compose_config(
    base_text: str,
    merge_text: str,
    script_text: str,
    *,
    script_timeout_s: float = _DEFAULT_SCRIPT_TIMEOUT_S,
    memory_limit_mb: int = _DEFAULT_MEMORY_LIMIT_MB,
) -> tuple[str, list[str]]:
    """Async wrapper for :func:`compose_sync`, off the event loop.

    QuickJS blocks its thread, and a large config's YAML round-trip is not free
    either, so the whole pipeline runs in a worker thread. The script's own time
    limit (not ``asyncio``) is what bounds a runaway script.
    """
    return await asyncio.to_thread(
        compose_sync,
        base_text,
        merge_text,
        script_text,
        script_timeout_s=script_timeout_s,
        memory_limit_mb=memory_limit_mb,
    )
