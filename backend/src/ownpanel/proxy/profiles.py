"""Subscription profiles and the panel-managed extend config/script.

A *profile* is a remote Clash config (a "订阅"): a URL plus an optional refresh
interval. The panel keeps every profile's raw text on disk and marks one as
*active*; the active profile's raw text is the base document of the synthesis
pipeline in :mod:`ownpanel.proxy.compose`, whose output is written to
``settings.proxy.clash_config`` and reloaded.

State lives in ``data_dir`` as plain files, following the precedent set by
:mod:`ownpanel.proxy.custom_rules` (no DB table, no migration):

* ``proxy_profiles.json``      — the profile list (metadata only)
* ``proxy_profiles/<id>.yaml`` — each profile's raw downloaded text
* ``proxy_merge.yaml``         — the extend config (one, global)
* ``proxy_script.js``          — the extend script (one, global)

Writes go through :func:`ownpanel.proxy.custom_rules.atomic_write_text`, so a
crash mid-write can never leave a truncated config behind.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from ownpanel.config import Settings
from ownpanel.errors import BadRequestError, ConflictError, HelperUnavailableError, NotFoundError
from ownpanel.logging import get_logger
from ownpanel.proxy.clash import ClashClient
from ownpanel.proxy.compose import compose_config
from ownpanel.proxy.custom_rules import atomic_write_text, inject_rules, load_rules

log = get_logger(__name__)

PROFILES_FILE = "proxy_profiles.json"
PROFILES_DIR = "proxy_profiles"
MERGE_FILE = "proxy_merge.yaml"
SCRIPT_FILE = "proxy_script.js"

#: Upper bound on a downloaded subscription, matching the config-file API's cap.
MAX_SUBSCRIPTION_BYTES = 4 * 1024 * 1024

#: Refresh interval bounds, in minutes (0 = never auto-update).
MIN_INTERVAL_MIN = 5
MAX_INTERVAL_MIN = 7 * 24 * 60

_NAME_MAX = 128
_URL_MAX = 2048
_UA_MAX = 200

#: ``subscription-userinfo: upload=1; download=2; total=3; expire=4``
_USERINFO_FIELD = re.compile(r"([a-z_]+)\s*=\s*(\d+)")


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def profiles_path(settings: Settings) -> Path:
    return settings.paths.data_dir / PROFILES_FILE


def profiles_dir(settings: Settings) -> Path:
    return settings.paths.data_dir / PROFILES_DIR


def raw_path(settings: Settings, profile_id: str) -> Path:
    return profiles_dir(settings) / f"{profile_id}.yaml"


def merge_path(settings: Settings) -> Path:
    return settings.paths.data_dir / MERGE_FILE


def script_path(settings: Settings) -> Path:
    return settings.paths.data_dir / SCRIPT_FILE


def _read_text(path: Path) -> str:
    """File contents, or ``""`` when it does not exist / cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------------------
# profile list persistence
# ---------------------------------------------------------------------------


def _shape(item: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one stored record; ``None`` when it is unusable."""
    profile_id = _as_str(item.get("id"))
    url = _as_str(item.get("url"))
    if not profile_id or not url:
        return None
    info = item.get("subscription")
    return {
        "id": profile_id,
        "name": _as_str(item.get("name")) or profile_id,
        "url": url,
        "user_agent": _as_str(item.get("user_agent")),
        "interval_min": _as_int(item.get("interval_min")),
        "active": bool(item.get("active")),
        "last_updated": _as_str(item.get("last_updated")),
        "last_error": _as_str(item.get("last_error")),
        "subscription": info if isinstance(info, dict) else {},
    }


def load_profiles(settings: Settings) -> list[dict[str, Any]]:
    """Stored profiles (empty when the file is absent or corrupt)."""
    try:
        data = json.loads(profiles_path(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            shaped = _shape(item)
            if shaped is not None:
                out.append(shaped)
    return out


def save_profiles(settings: Settings, profiles: list[dict[str, Any]]) -> None:
    """Atomically persist the profile list."""
    settings.paths.data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    atomic_write_text(profiles_path(settings), json.dumps(profiles, ensure_ascii=False, indent=2))


def find_profile(profiles: list[dict[str, Any]], profile_id: str) -> dict[str, Any]:
    for profile in profiles:
        if profile["id"] == profile_id:
            return profile
    raise NotFoundError(f"订阅不存在: {profile_id}")


def active_profile(profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The profile currently driving the config file, if any."""
    for profile in profiles:
        if profile["active"]:
            return profile
    return None


def set_active(profiles: list[dict[str, Any]], profile_id: str) -> None:
    """Mark exactly one profile active (in place)."""
    for profile in profiles:
        profile["active"] = profile["id"] == profile_id


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate_url(url: str) -> str:
    """Reject anything but an ``http(s)://`` subscription URL."""
    cleaned = url.strip()
    if not cleaned:
        raise BadRequestError("订阅地址不能为空")
    if len(cleaned) > _URL_MAX:
        raise BadRequestError(f"订阅地址过长(最多 {_URL_MAX} 字符)")
    if not cleaned.lower().startswith(("http://", "https://")):
        raise BadRequestError("订阅地址必须以 http:// 或 https:// 开头")
    return cleaned


def validate_interval(minutes: int) -> int:
    """Clamp/validate the refresh interval; ``0`` means never auto-update."""
    if minutes == 0:
        return 0
    if minutes < MIN_INTERVAL_MIN or minutes > MAX_INTERVAL_MIN:
        raise BadRequestError(
            f"更新间隔需为 0(不自动更新)或 {MIN_INTERVAL_MIN}~{MAX_INTERVAL_MIN} 分钟"
        )
    return minutes


def validate_name(name: str) -> str:
    cleaned = name.strip()
    if len(cleaned) > _NAME_MAX:
        raise BadRequestError(f"名称过长(最多 {_NAME_MAX} 字符)")
    return cleaned


def validate_user_agent(user_agent: str) -> str:
    cleaned = user_agent.strip()
    if len(cleaned) > _UA_MAX:
        raise BadRequestError(f"User-Agent 过长(最多 {_UA_MAX} 字符)")
    return cleaned


def new_profile(
    *, name: str, url: str, interval_min: int, user_agent: str, active: bool
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "name": validate_name(name) or "订阅",
        "url": validate_url(url),
        "user_agent": validate_user_agent(user_agent),
        "interval_min": validate_interval(interval_min),
        "active": active,
        "last_updated": "",
        "last_error": "",
        "subscription": {},
    }


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


def parse_userinfo(header: str) -> dict[str, int]:
    """Parse a ``subscription-userinfo`` header into upload/download/total/expire.

    Airports report quota through this de-facto-standard header; missing fields
    are simply absent from the result.
    """
    fields = {key: int(value) for key, value in _USERINFO_FIELD.findall(header.lower())}
    return {key: fields[key] for key in ("upload", "download", "total", "expire") if key in fields}


#: Cap on the connect phase of a download attempt. The direct attempt runs
#: first, and a blocked dashboard fails at TCP connect — capping just that phase
#: keeps the fallback quick without shortening the read budget for a slow but
#: working link.
_CONNECT_TIMEOUT_S = 10.0


def make_client(settings: Settings, user_agent: str, proxy: str = "") -> httpx.AsyncClient:
    """HTTP client for subscription downloads (one home for timeout/UA/redirects).

    Kept as a separate function so tests can swap in a ``MockTransport`` without
    monkeypatching ``httpx`` globally.
    """
    total = settings.proxy.subscription_timeout_s
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=min(_CONNECT_TIMEOUT_S, total), read=total, write=total, pool=total
        ),
        follow_redirects=True,
        headers={"User-Agent": user_agent},
        proxy=proxy or None,
    )


async def discover_local_proxy(settings: Settings) -> str:
    """The running core's own listen port as an ``http://`` proxy URL.

    Asks the controller for its live ``mixed-port`` (or plain ``port``) rather
    than parsing the config file, so it stays right after a port change. Returns
    ``""`` when the controller is unreachable or listens on neither.
    """
    client = ClashClient(settings.proxy.clash_api, settings.proxy.clash_secret)
    try:
        configs = await client.configs()
    except httpx.HTTPError:
        return ""
    finally:
        await client.aclose()
    port = _as_int(configs.get("mixed-port")) or _as_int(configs.get("port"))
    if not port:
        return ""
    # The controller is addressed on loopback, and so is the port it reports.
    return f"http://127.0.0.1:{port}"


async def resolve_fallback_proxy(settings: Settings) -> str:
    """Retry proxy per ``proxy.subscription_fallback_proxy`` (``""`` = no retry)."""
    setting = settings.proxy.subscription_fallback_proxy.strip()
    if setting.lower() in ("", "none", "direct", "off"):
        return ""
    if setting.lower() == "auto":
        return await discover_local_proxy(settings)
    return setting


def _http_error_text(exc: httpx.HTTPError) -> str:
    """Readable text for an httpx failure.

    Several httpx exceptions (``ConnectTimeout`` above all) stringify to the
    empty string, which would leave the user with a bare "下载订阅失败: ". Fall
    back to the exception class name so the message always says something.
    """
    return str(exc) or type(exc).__name__


async def _download(
    settings: Settings, url: str, user_agent: str, proxy: str
) -> tuple[str, dict[str, int]]:
    """One download attempt; raises BadRequestError on any failure."""
    try:
        async with (
            make_client(settings, user_agent, proxy) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise BadRequestError(f"订阅返回 HTTP {resp.status_code}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_SUBSCRIPTION_BYTES:
                    raise BadRequestError(
                        f"订阅内容超过 {MAX_SUBSCRIPTION_BYTES // (1024 * 1024)} MB 上限"
                    )
                chunks.append(chunk)
            info = parse_userinfo(resp.headers.get("subscription-userinfo", ""))
    except httpx.HTTPError as exc:
        raise BadRequestError(f"下载订阅失败: {_http_error_text(exc)}") from exc

    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadRequestError("订阅内容不是 UTF-8 文本") from exc
    if not text.strip():
        raise BadRequestError("订阅内容为空")
    return text, info


async def fetch_subscription(
    settings: Settings, profile: dict[str, Any]
) -> tuple[str, dict[str, int]]:
    """Download a profile's remote config; returns (text, subscription info).

    Tries a direct connection first: it is the fast path (typically ~1s) and the
    only one that can work on a fresh install, where the local core has no usable
    nodes to route through. If that fails — airport dashboards are routinely
    blocked on exactly the network that needs the subscription — the download is
    retried through ``proxy.subscription_fallback_proxy``. When both fail the
    error names each attempt.

    Raises:
        BadRequestError: transport failure, non-2xx status, oversized body, or a
            response that is not decodable text.
    """
    url = validate_url(profile["url"])
    user_agent = profile.get("user_agent") or settings.proxy.subscription_user_agent
    try:
        return await _download(settings, url, user_agent, "")
    except BadRequestError as direct:
        proxy = await resolve_fallback_proxy(settings)
        if not proxy:
            raise
        log.info("proxy.subscription_proxy_retry", url=url, error=str(direct))
        try:
            return await _download(settings, url, user_agent, proxy)
        except BadRequestError as via_proxy:
            raise BadRequestError(
                f"直连失败: {direct}; 经代理({proxy})也失败: {via_proxy}"
            ) from via_proxy


def store_raw(settings: Settings, profile_id: str, text: str) -> None:
    """Persist a profile's downloaded text (the pipeline's base document)."""
    directory = profiles_dir(settings)
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    atomic_write_text(raw_path(settings, profile_id), text)


def load_raw(settings: Settings, profile_id: str) -> str:
    return _read_text(raw_path(settings, profile_id))


def drop_raw(settings: Settings, profile_id: str) -> None:
    with contextlib.suppress(OSError):
        raw_path(settings, profile_id).unlink()


# ---------------------------------------------------------------------------
# extend config / script
# ---------------------------------------------------------------------------


def load_extend(settings: Settings) -> dict[str, str]:
    """The extend config (merge YAML) and extend script (JS), possibly empty."""
    return {
        "merge": _read_text(merge_path(settings)),
        "script": _read_text(script_path(settings)),
    }


def save_extend(settings: Settings, *, merge: str | None, script: str | None) -> None:
    """Persist the extend config/script (``None`` leaves that one untouched)."""
    settings.paths.data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    if merge is not None:
        atomic_write_text(merge_path(settings), merge)
    if script is not None:
        atomic_write_text(script_path(settings), script)


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_due(profile: dict[str, Any], now: datetime) -> bool:
    """Whether ``profile``'s refresh interval has elapsed.

    A profile that has never been updated successfully is due immediately, so a
    subscription added while the network was down gets picked up by the next
    scheduler tick instead of waiting a full interval.
    """
    interval = _as_int(profile.get("interval_min"))
    if interval <= 0:
        return False
    last = _parse_ts(_as_str(profile.get("last_updated")))
    if last is None:
        return True
    return now - last >= timedelta(minutes=interval)


def due_profiles(profiles: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Profiles whose auto-update is due at ``now``."""
    return [profile for profile in profiles if is_due(profile, now)]


# ---------------------------------------------------------------------------
# applying the pipeline output
# ---------------------------------------------------------------------------


def config_target(settings: Settings) -> Path:
    path = settings.proxy.clash_config
    if path is None:
        raise ConflictError("未配置 Clash 配置文件路径 (proxy.clash_config)")
    return path


def finalize(settings: Settings, composed: str) -> str:
    """Re-inject panel-managed custom rules into a freshly composed config.

    Custom rules are a separate feature that must survive every regeneration,
    exactly as they survive a manual config upload (``PUT /proxy/config-file``).
    """
    return inject_rules(composed, [str(rule["rule"]) for rule in load_rules(settings)])


def mark_updated(profile: dict[str, Any], info: dict[str, int]) -> None:
    profile["last_updated"] = utcnow().isoformat()
    profile["last_error"] = ""
    if info:
        profile["subscription"] = info


def mark_failed(profile: dict[str, Any], message: str) -> None:
    profile["last_error"] = message[:500]


async def write_and_reload(settings: Settings, content: str) -> Path:
    """Validate, atomically write and reload the Clash config file.

    On a reload failure the previous file content is restored: the controller
    keeps running its in-memory config either way, so the rollback needs no
    second reload. Mirrors ``_sync_rules_to_config`` in the router.

    Raises:
        BadRequestError: invalid YAML or an unwritable target.
        HelperUnavailableError: the controller refused or is unreachable.
    """
    path = config_target(settings)
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise BadRequestError(f"生成的配置不是合法 YAML: {exc}") from exc

    original: str | None = None
    with contextlib.suppress(OSError):
        original = path.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)

    client = ClashClient(settings.proxy.clash_api, settings.proxy.clash_secret)
    try:
        await client.reload_configs(path=str(path))
    except httpx.HTTPError as exc:
        if original is not None:
            with contextlib.suppress(BadRequestError):
                atomic_write_text(path, original)
        raise HelperUnavailableError(f"重载 Clash 配置失败: {exc}") from exc
    finally:
        await client.aclose()
    return path


async def apply_profile(settings: Settings, profile: dict[str, Any]) -> tuple[Path, list[str]]:
    """Run the full pipeline for ``profile`` and make the result live.

    Returns the written path plus the extend script's ``console`` output.
    """
    base = load_raw(settings, profile["id"])
    if not base.strip():
        raise BadRequestError("该订阅还没有下载过内容, 请先更新")
    extend = load_extend(settings)
    composed, logs = await compose_config(
        base,
        extend["merge"],
        extend["script"],
        script_timeout_s=settings.proxy.script_timeout_s,
        memory_limit_mb=settings.proxy.script_memory_limit_mb,
    )
    path = await write_and_reload(settings, finalize(settings, composed))
    return path, logs


async def apply_active(settings: Settings, profiles: list[dict[str, Any]]) -> list[str]:
    """Regenerate the config from the active profile; no-op when none is set."""
    profile = active_profile(profiles)
    if profile is None:
        return []
    _, logs = await apply_profile(settings, profile)
    return logs
