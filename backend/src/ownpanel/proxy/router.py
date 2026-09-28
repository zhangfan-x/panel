"""Proxy management API: Clash/Mihomo controller status, groups, mode & config.

All controller traffic goes through :class:`ownpanel.proxy.clash.ClashClient`
(localhost HTTP, optional Bearer). The Clash config file is owned by the API
user, so ``/proxy/config-file`` reads/writes it directly (no root helper) and
triggers a controller reload after an atomic, YAML-validated save. Controller
connectivity failures map to :class:`HelperUnavailableError` (502), the closest
existing "upstream service unavailable" contract (docs/code-plan-m2-m5.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import yaml
from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from ownpanel.audit import record_audit
from ownpanel.auth.deps import (
    ActiveSessionDep,
    CurrentAuth,
    DbDep,
    SettingsDep,
    require_csrf,
    require_recent_reauth,
    resolve_stream_auth,
)
from ownpanel.config import Settings
from ownpanel.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    HelperUnavailableError,
    NotFoundError,
)
from ownpanel.events import SSE_HEADERS
from ownpanel.logging import get_logger
from ownpanel.proxy import profiles
from ownpanel.proxy.clash import ClashClient, parse_groups, parse_status
from ownpanel.proxy.compose import compose_config
from ownpanel.proxy.custom_rules import (
    atomic_write_text,
    inject_rules,
    load_rules,
    new_rule_entry,
    save_rules,
    validate_rule,
)
from ownpanel.proxy.tracker import ProxyTracker

log = get_logger(__name__)

proxy_router = APIRouter(prefix="/proxy", tags=["proxy"])

_CONFIG_MAX_BYTES = 4 * 1024 * 1024

NameParam = Annotated[str, PathParam(min_length=1, max_length=256)]

ProxyMode = Literal["rule", "global", "direct"]


class SelectRequest(BaseModel):
    """Body for selecting a node inside a group."""

    name: str = Field(min_length=1, max_length=256)


class ConfigPatchRequest(BaseModel):
    """Body for a live config patch (currently only the proxy mode)."""

    mode: ProxyMode | None = None


class ConfigFileRequest(BaseModel):
    """Body for a full config-file replacement (validated as YAML before write)."""

    content: str = Field(max_length=_CONFIG_MAX_BYTES)


def _client(settings: Settings) -> ClashClient:
    return ClashClient(settings.proxy.clash_api, settings.proxy.clash_secret)


def _controller_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str):
            return message
    return ""


async def _call[T](settings: Settings, fn: Callable[[ClashClient], Awaitable[T]]) -> T:
    """Run a controller call, mapping httpx failures to a 502 AppError."""
    client = _client(settings)
    try:
        return await fn(client)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = _controller_message(exc.response) or f"Clash controller returned {status}"
        raise HelperUnavailableError(detail) from exc
    except httpx.HTTPError as exc:
        raise HelperUnavailableError(
            "Clash controller is unreachable; is the proxy service running?"
        ) from exc
    finally:
        await client.aclose()


def _tracker(request: Request) -> ProxyTracker:
    tracker: ProxyTracker = request.app.state.proxy_tracker
    return tracker


@contextlib.asynccontextmanager
async def _audited(
    db: AsyncSession,
    auth: CurrentAuth,
    request: Request,
    *,
    action: str,
    target: str = "",
) -> AsyncIterator[None]:
    """Audit the wrapped mutation: one ``ok`` row on success, ``denied`` on AppError."""
    try:
        yield
    except AppError as exc:
        await record_audit(
            db,
            resource="proxy",
            action=action,
            result="denied",
            target=target,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="proxy",
        action=action,
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )


# ---------------------------------------------------------------------------
# systemd user service (clash core on/off)
# ---------------------------------------------------------------------------

ServiceAction = Literal["start", "stop", "restart"]

_SYSTEMCTL_TIMEOUT_S = 30.0


class ServiceActionRequest(BaseModel):
    """Body for switching the clash core service on/off."""

    action: ServiceAction


async def _run_systemctl(settings: Settings, *args: str) -> tuple[int, str]:
    """Run ``systemctl --user <args> <clash_service>``; returns (rc, output).

    The clash core runs as a per-user systemd unit owned by the API user
    (config.py ProxyConfig), so no root helper is involved. A missing
    ``systemctl`` or a timeout degrades to a non-zero rc with a message.
    """
    cmd = ("systemctl", "--user", *args, settings.proxy.clash_service)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as exc:
        return 127, f"systemctl unavailable: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_SYSTEMCTL_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return 124, "systemctl timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


def _service_state(rc: int, output: str) -> str:
    """First line of ``is-active`` output (active/inactive/failed/…)."""
    first = output.splitlines()[0].strip() if output else ""
    return first or ("active" if rc == 0 else "unknown")


@proxy_router.get("/service")
async def proxy_service_status(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Clash core service state (independent of controller reachability)."""
    rc, out = await _run_systemctl(settings, "is-active")
    return {
        "service": settings.proxy.clash_service,
        "running": rc == 0,
        "state": _service_state(rc, out),
    }


@proxy_router.post("/service", dependencies=[Depends(require_csrf)])
async def proxy_service_action(
    body: ServiceActionRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Start/stop/restart the clash core service (CSRF required)."""
    async with _audited(db, auth, request, action="proxy.service", target=body.action):
        rc, out = await _run_systemctl(settings, body.action)
        if rc != 0:
            raise BadRequestError(out or f"systemctl {body.action} failed (rc={rc})")
    rc, out = await _run_systemctl(settings, "is-active")
    return {
        "ok": True,
        "action": body.action,
        "running": rc == 0,
        "state": _service_state(rc, out),
    }


@proxy_router.get("/status")
async def proxy_status(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Controller version + live config summary (mode, listen ports…)."""

    async def run(client: ClashClient) -> dict[str, Any]:
        version = await client.version()
        configs = await client.configs()
        return parse_status(version, configs)

    return await _call(settings, run)


@proxy_router.get("/groups")
async def proxy_groups(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Selectable proxy groups with their current node and candidate nodes."""
    proxies = await _call(settings, lambda c: c.proxies())
    return {"groups": parse_groups(proxies)}


@proxy_router.post("/groups/{group}/select", dependencies=[Depends(require_csrf)])
async def proxy_select(
    group: NameParam,
    body: SelectRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Select a node inside a group (``PUT /proxies/{group}``; CSRF required)."""
    try:
        await _call(settings, lambda c: c.select(group, body.name))
    except AppError as exc:
        await record_audit(
            db,
            resource="proxy",
            action="proxy.select",
            result="denied",
            target=group,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="proxy",
        action="proxy.select",
        target=group,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"ok": True, "group": group, "now": body.name}


@proxy_router.get("/proxies/{name}/delay")
async def proxy_delay(
    name: NameParam,
    settings: SettingsDep,
    _auth: ActiveSessionDep,
    timeout: Annotated[int, Query(ge=100, le=30000)] = 5000,
) -> dict[str, Any]:
    """Latency test for one proxy against the configured probe endpoint."""
    url = settings.proxy.delay_test_url
    return await _call(settings, lambda c: c.delay(name, timeout_ms=timeout, url=url))


#: Cap on concurrent per-node probes when falling back from the batch endpoint.
_DELAY_FALLBACK_CONCURRENCY = 8


async def _delay_each(
    client: ClashClient, members: list[str], *, timeout_ms: int, url: str
) -> dict[str, int]:
    """Probe members one by one with bounded concurrency (pre-``/group`` cores)."""
    limit = asyncio.Semaphore(_DELAY_FALLBACK_CONCURRENCY)
    results: dict[str, int] = {}

    async def probe(member: str) -> None:
        async with limit:
            with contextlib.suppress(httpx.HTTPError):
                result = await client.delay(member, timeout_ms=timeout_ms, url=url)
                delay = int(result.get("delay") or 0)
                if delay:
                    results[member] = delay

    await asyncio.gather(*(probe(member) for member in members))
    return results


@proxy_router.get("/groups/{group}/delay")
async def proxy_group_delay(
    group: NameParam,
    settings: SettingsDep,
    _auth: ActiveSessionDep,
    timeout: Annotated[int, Query(ge=100, le=30000)] = 5000,
) -> dict[str, Any]:
    """Latency test for every member of a group in one controller round-trip.

    Mihomo's ``/group/{name}/delay`` probes the whole group server-side, which
    replaces one HTTP request per member (a 100-node group used to mean 100
    concurrent requests from the browser). Cores without that endpoint fall
    back to bounded per-node probes. Members that timed out are omitted from
    ``delays``.
    """

    async def run(client: ClashClient) -> dict[str, Any]:
        info = await client.proxy(group)
        members = [m for m in (info.get("all") or []) if isinstance(m, str)]
        if not members:
            raise NotFoundError(f"代理组不存在或没有成员: {group}")
        # The group's own testUrl is what its automatic tests use; matching it
        # keeps manual results consistent with the delays shown on load.
        configured = info.get("testUrl")
        url = configured if isinstance(configured, str) and configured else ""
        url = url or settings.proxy.delay_test_url
        batch = await client.group_delay(group, timeout_ms=timeout, url=url)
        if batch is None:
            return {
                "delays": await _delay_each(client, members, timeout_ms=timeout, url=url),
                "batched": False,
            }
        return {"delays": batch, "batched": True}

    return await _call(settings, run)


@proxy_router.patch("/config", dependencies=[Depends(require_csrf)])
async def proxy_patch_config(
    body: ConfigPatchRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Patch live config — switch rule/global/direct mode (CSRF required)."""
    payload: dict[str, Any] = {}
    if body.mode is not None:
        payload["mode"] = body.mode
    if not payload:
        raise BadRequestError("no config field to update")
    try:
        await _call(settings, lambda c: c.patch_configs(payload))
    except AppError as exc:
        await record_audit(
            db,
            resource="proxy",
            action="proxy.config",
            result="denied",
            target=body.mode or "",
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="proxy",
        action="proxy.config",
        target=body.mode or "",
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"ok": True, **payload}


@proxy_router.get("/rules")
async def proxy_rules(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Active routing rules."""
    return await _call(settings, lambda c: c.rules())


# ---------------------------------------------------------------------------
# custom rules (panel-managed, survive config-file uploads)
# ---------------------------------------------------------------------------

RuleIdParam = Annotated[str, PathParam(pattern=r"^[a-f0-9]{1,32}$")]


class RuleCreateRequest(BaseModel):
    """Body for adding one custom rule (Clash ``TYPE,ARGUMENT,POLICY`` string)."""

    rule: str = Field(min_length=1, max_length=512)


async def _sync_rules_to_config(settings: Settings, rules: list[dict[str, Any]]) -> None:
    """Re-inject the custom-rule block into the config file and reload.

    On a reload failure the previous file content is restored — the controller
    keeps running its in-memory config either way, so no second reload is
    needed for the rollback.
    """
    path = _config_path(settings)
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotFoundError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise BadRequestError(f"failed to read config file: {exc}") from exc
    injected = inject_rules(original, [str(r["rule"]) for r in rules])
    _validate_yaml(injected)
    atomic_write_text(path, injected)
    try:
        await _call(settings, lambda c: c.reload_configs(path=str(path)))
    except AppError:
        with contextlib.suppress(BadRequestError):
            atomic_write_text(path, original)
        raise


@proxy_router.get("/rules/custom")
async def proxy_custom_rules(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Panel-managed custom rules (injected ahead of the config's own rules)."""
    return {"rules": load_rules(settings)}


@proxy_router.post("/rules/custom", dependencies=[Depends(require_csrf)])
async def proxy_add_custom_rule(
    body: RuleCreateRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Add a custom rule, inject it into the config file and reload (CSRF)."""
    async with _audited(db, auth, request, action="proxy.rule_add", target=body.rule[:200]):
        rule = validate_rule(body.rule)
        rules = load_rules(settings)
        if any(r["rule"] == rule for r in rules):
            raise ConflictError("该规则已存在")
        entry = new_rule_entry(rule)
        rules.append(entry)
        # Sync the config first: if injection/reload fails the JSON stays unchanged.
        await _sync_rules_to_config(settings, rules)
        save_rules(settings, rules)
    return {"ok": True, "rule": entry}


@proxy_router.delete("/rules/custom/{rule_id}", dependencies=[Depends(require_csrf)])
async def proxy_delete_custom_rule(
    rule_id: RuleIdParam,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Remove a custom rule and re-sync the config file (CSRF required)."""
    async with _audited(db, auth, request, action="proxy.rule_remove", target=rule_id):
        rules = load_rules(settings)
        remaining = [r for r in rules if r["id"] != rule_id]
        if len(remaining) == len(rules):
            raise NotFoundError("自定义规则不存在")
        await _sync_rules_to_config(settings, remaining)
        save_rules(settings, remaining)
    return {"ok": True}


@proxy_router.get("/connections")
async def proxy_connections(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Live active connections plus the last 100 closed ones (panel tracker).

    Served from the background :class:`ProxyTracker` snapshot (≈2s freshness);
    ``available=false`` means the controller is currently unreachable.
    """
    return _tracker(request).connections()


@proxy_router.delete("/connections/{conn_id}", dependencies=[Depends(require_csrf)])
async def proxy_close_connection(
    conn_id: NameParam,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Close one connection by id (CSRF required)."""
    async with _audited(db, auth, request, action="proxy.connection_close", target=conn_id):
        await _call(settings, lambda c: c.close_connection(conn_id))
    return {"ok": True}


@proxy_router.delete("/connections", dependencies=[Depends(require_csrf)])
async def proxy_close_connections(
    request: Request, settings: SettingsDep, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Close all open connections (CSRF required)."""
    try:
        await _call(settings, lambda c: c.close_connections())
    except AppError as exc:
        await record_audit(
            db,
            resource="proxy",
            action="proxy.connections_clear",
            result="denied",
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="proxy",
        action="proxy.connections_clear",
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"ok": True}


def _config_path(settings: Settings) -> Path:
    path = settings.proxy.clash_config
    if path is None:
        raise ConflictError("Clash config file path is not configured (proxy.clash_config)")
    return path


@proxy_router.get("/config-file")
async def proxy_get_config_file(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Read the Clash config file text (owned by the API user; read directly)."""
    path = _config_path(settings)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotFoundError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise BadRequestError(f"failed to read config file: {exc}") from exc
    return {"path": str(path), "content": content}


@proxy_router.put(
    "/config-file",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def proxy_put_config_file(
    body: ConfigFileRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Validate, atomically write and reload the Clash config file (CSRF + reauth).

    Panel-managed custom rules are re-injected into the uploaded content before
    the write, so a new config file never wipes them out.
    """
    path = _config_path(settings)
    try:
        content = inject_rules(body.content, [str(r["rule"]) for r in load_rules(settings)])
        _validate_yaml(content)
        atomic_write_text(path, content)
        await _call(settings, lambda c: c.reload_configs(path=str(path)))
    except AppError as exc:
        await record_audit(
            db,
            resource="proxy",
            action="proxy.config_file_write",
            result="denied",
            target=str(path),
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="proxy",
        action="proxy.config_file_write",
        target=str(path),
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"ok": True, "path": str(path)}


def _validate_yaml(content: str) -> None:
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise BadRequestError(f"invalid YAML: {exc}") from exc


def _num(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sse(topic: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {topic}\ndata: {payload}\n\n"


async def _traffic_source(request: Request, settings: Settings) -> AsyncIterator[str]:
    """Forward the controller's ``/traffic`` stream as SSE ``traffic`` events."""
    client = _client(settings)
    try:
        yield _sse("ready", {})
        async with client.stream_traffic() as lines:
            async for raw in lines:
                if await request.is_disconnected():
                    return
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    yield _sse(
                        "traffic",
                        {"up": _num(data.get("up")), "down": _num(data.get("down"))},
                    )
    except httpx.HTTPError:
        # Controller unreachable/closed: end the stream; GET /proxy/status surfaces the error.
        log.info("proxy.traffic_stream_unavailable")
        return
    finally:
        await client.aclose()


@proxy_router.get("/traffic/stream")
async def proxy_traffic_stream(
    request: Request,
    _auth: Annotated[CurrentAuth, Depends(resolve_stream_auth)],
) -> StreamingResponse:
    """Live upload/download rate as a short-lived SSE stream (proxied from the controller)."""
    settings: Settings = request.app.state.settings
    return StreamingResponse(
        _traffic_source(request, settings),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@proxy_router.get("/traffic/history")
async def proxy_traffic_history(
    request: Request,
    _auth: ActiveSessionDep,
    minutes: Annotated[int, Query(ge=1, le=10080)] = 1440,
) -> dict[str, Any]:
    """Per-minute average up/down rates for the window (default 24h).

    Served from the tracker's in-memory ring (up to 7 days); an empty list
    simply means the panel has not observed traffic yet (fresh start or
    controller down).
    """
    tracker = _tracker(request)
    return {"samples": tracker.traffic_history(minutes), "latest": tracker.traffic_latest()}


# ---------------------------------------------------------------------------
# subscription profiles + extend config/script
# ---------------------------------------------------------------------------

ProfileIdParam = Annotated[str, PathParam(pattern=r"^[a-f0-9]{1,32}$")]

_EXTEND_MAX_BYTES = 1024 * 1024


class ProfileCreateRequest(BaseModel):
    """Body for adding a subscription profile."""

    name: str = Field(default="", max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    interval_min: int = Field(default=0, ge=0, le=profiles.MAX_INTERVAL_MIN)
    user_agent: str = Field(default="", max_length=200)


class ProfileUpdateRequest(BaseModel):
    """Body for editing a subscription profile (omitted fields stay as-is)."""

    name: str | None = Field(default=None, max_length=128)
    url: str | None = Field(default=None, max_length=2048)
    interval_min: int | None = Field(default=None, ge=0, le=profiles.MAX_INTERVAL_MIN)
    user_agent: str | None = Field(default=None, max_length=200)


class ExtendRequest(BaseModel):
    """Body for saving the extend config and/or extend script."""

    merge: str | None = Field(default=None, max_length=_EXTEND_MAX_BYTES)
    script: str | None = Field(default=None, max_length=_EXTEND_MAX_BYTES)


class ExtendPreviewRequest(BaseModel):
    """Body for a dry-run of the pipeline (nothing is written)."""

    merge: str = Field(default="", max_length=_EXTEND_MAX_BYTES)
    script: str = Field(default="", max_length=_EXTEND_MAX_BYTES)
    #: Which profile supplies the base document; the active one when omitted.
    profile_id: str = Field(default="", max_length=32)


def _profiles_body(items: list[dict[str, Any]]) -> dict[str, Any]:
    active = profiles.active_profile(items)
    return {"profiles": items, "active_id": active["id"] if active else ""}


@proxy_router.get("/profiles")
async def proxy_list_profiles(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Subscription profiles and which one currently drives the config file."""
    return _profiles_body(profiles.load_profiles(settings))


@proxy_router.post("/profiles", dependencies=[Depends(require_csrf)])
async def proxy_add_profile(
    body: ProfileCreateRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Add a subscription and download it immediately (CSRF required).

    The first profile added becomes the active one — with nothing else in play
    that is what the user means, and it makes the config regenerate right away.
    """
    async with _audited(db, auth, request, action="proxy.profile_add", target=body.url[:200]):
        items = profiles.load_profiles(settings)
        entry = profiles.new_profile(
            name=body.name,
            url=body.url,
            interval_min=body.interval_min,
            user_agent=body.user_agent,
            active=not items,
        )
        text, info = await profiles.fetch_subscription(settings, entry)
        profiles.store_raw(settings, entry["id"], text)
        profiles.mark_updated(entry, info)
        items.append(entry)
        if entry["active"]:
            await profiles.apply_profile(settings, entry)
        profiles.save_profiles(settings, items)
    return {"ok": True, "profile": entry}


@proxy_router.patch("/profiles/{profile_id}", dependencies=[Depends(require_csrf)])
async def proxy_update_profile(
    profile_id: ProfileIdParam,
    body: ProfileUpdateRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Edit a subscription's name/URL/interval/User-Agent (CSRF required)."""
    async with _audited(db, auth, request, action="proxy.profile_update", target=profile_id):
        items = profiles.load_profiles(settings)
        entry = profiles.find_profile(items, profile_id)
        if body.name is not None:
            entry["name"] = profiles.validate_name(body.name) or entry["name"]
        if body.url is not None:
            entry["url"] = profiles.validate_url(body.url)
        if body.interval_min is not None:
            entry["interval_min"] = profiles.validate_interval(body.interval_min)
        if body.user_agent is not None:
            entry["user_agent"] = profiles.validate_user_agent(body.user_agent)
        profiles.save_profiles(settings, items)
    return {"ok": True, "profile": entry}


@proxy_router.delete("/profiles/{profile_id}", dependencies=[Depends(require_csrf)])
async def proxy_delete_profile(
    profile_id: ProfileIdParam,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Delete a subscription and its cached content (CSRF required).

    The generated config file is left alone: dropping a profile should not take
    the proxy offline. Activate another profile to regenerate it.
    """
    async with _audited(db, auth, request, action="proxy.profile_remove", target=profile_id):
        items = profiles.load_profiles(settings)
        profiles.find_profile(items, profile_id)
        remaining = [item for item in items if item["id"] != profile_id]
        profiles.drop_raw(settings, profile_id)
        profiles.save_profiles(settings, remaining)
    return _profiles_body(remaining)


@proxy_router.post("/profiles/{profile_id}/update", dependencies=[Depends(require_csrf)])
async def proxy_refresh_profile(
    profile_id: ProfileIdParam,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Re-download a subscription now; regenerate the config when it is active."""
    async with _audited(db, auth, request, action="proxy.profile_refresh", target=profile_id):
        items = profiles.load_profiles(settings)
        entry = profiles.find_profile(items, profile_id)
        try:
            text, info = await profiles.fetch_subscription(settings, entry)
        except AppError as exc:
            # Persist the failure so the UI can show why the last run did not work.
            profiles.mark_failed(entry, exc.detail or exc.title)
            profiles.save_profiles(settings, items)
            raise
        profiles.store_raw(settings, entry["id"], text)
        profiles.mark_updated(entry, info)
        logs: list[str] = []
        if entry["active"]:
            _, logs = await profiles.apply_profile(settings, entry)
        profiles.save_profiles(settings, items)
    return {"ok": True, "profile": entry, "logs": logs}


@proxy_router.post("/profiles/{profile_id}/activate", dependencies=[Depends(require_csrf)])
async def proxy_activate_profile(
    profile_id: ProfileIdParam,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Make a subscription the active one and regenerate the config file."""
    async with _audited(db, auth, request, action="proxy.profile_activate", target=profile_id):
        items = profiles.load_profiles(settings)
        entry = profiles.find_profile(items, profile_id)
        profiles.set_active(items, profile_id)
        _, logs = await profiles.apply_profile(settings, entry)
        profiles.save_profiles(settings, items)
    return {"ok": True, **_profiles_body(items), "logs": logs}


@proxy_router.get("/extend")
async def proxy_get_extend(settings: SettingsDep, _auth: ActiveSessionDep) -> dict[str, Any]:
    """The extend config (merge YAML) and extend script (JavaScript)."""
    return profiles.load_extend(settings)


@proxy_router.post("/extend/preview", dependencies=[Depends(require_csrf)])
async def proxy_preview_extend(
    body: ExtendPreviewRequest, settings: SettingsDep, _auth: ActiveSessionDep
) -> dict[str, Any]:
    """Dry-run the pipeline and return the composed YAML plus script output.

    Nothing is written and the controller is not touched, so this is the safe
    way to iterate on a script before committing to it.
    """
    items = profiles.load_profiles(settings)
    entry = (
        profiles.find_profile(items, body.profile_id)
        if body.profile_id
        else profiles.active_profile(items)
    )
    if entry is None:
        raise ConflictError("还没有可用的订阅, 请先添加一个订阅")
    base = profiles.load_raw(settings, entry["id"])
    if not base.strip():
        raise BadRequestError("该订阅还没有下载过内容, 请先更新")
    composed, logs = await compose_config(
        base,
        body.merge,
        body.script,
        script_timeout_s=settings.proxy.script_timeout_s,
        memory_limit_mb=settings.proxy.script_memory_limit_mb,
    )
    return {"content": profiles.finalize(settings, composed), "logs": logs}


@proxy_router.put(
    "/extend",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def proxy_put_extend(
    body: ExtendRequest,
    request: Request,
    settings: SettingsDep,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Save the extend config/script and regenerate the config (CSRF + reauth).

    Reauth is required because the extend script is arbitrary code the panel
    will run on every subscription update — the same bar as writing the config
    file directly.
    """
    async with _audited(db, auth, request, action="proxy.extend_write"):
        items = profiles.load_profiles(settings)
        entry = profiles.active_profile(items)
        if entry is not None:
            # Compose before persisting: a broken merge/script must not become
            # the stored state that every later update would replay.
            base = profiles.load_raw(settings, entry["id"])
            if base.strip():
                current = profiles.load_extend(settings)
                composed, _ = await compose_config(
                    base,
                    current["merge"] if body.merge is None else body.merge,
                    current["script"] if body.script is None else body.script,
                    script_timeout_s=settings.proxy.script_timeout_s,
                    memory_limit_mb=settings.proxy.script_memory_limit_mb,
                )
                await profiles.write_and_reload(settings, profiles.finalize(settings, composed))
        profiles.save_extend(settings, merge=body.merge, script=body.script)
    return {"ok": True, **profiles.load_extend(settings)}
