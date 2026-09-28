"""Docker API endpoints: info, containers, images, networks, volumes (§4.5).

All engine access goes through the root helper's ``docker.*`` actions
(docker-py); the API process never imports docker. Helper error codes are
mapped to semantic HTTP statuses and the whole router degrades to
``503 docker.unavailable`` when the daemon is unreachable. Response field
names are frontend contract; do not rename them. The optional
``WS /docker/containers/{id}/exec`` bridge is deferred to M5 (the helper-side
``docker.exec_create`` action already exists).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import psutil
from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from mypanel.audit import record_audit
from mypanel.auth.deps import (
    COOKIE_NAME,
    ActiveSessionDep,
    CurrentAuth,
    DbDep,
    get_auth_service,
    require_csrf,
    require_recent_reauth,
    resolve_stream_auth,
)
from mypanel.auth.service import AuthService
from mypanel.config import Settings
from mypanel.db.models import Job, SessionState
from mypanel.errors import (
    AppError,
    ConflictError,
    DockerUnavailableError,
    HelperActionError,
    HelperUnavailableError,
    NotFoundError,
    ReauthRequiredError,
)
from mypanel.events import SSE_HEADERS
from mypanel.ipc.client import HelperClient
from mypanel.ipc.fdpass import close_fds
from mypanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_DOCKER_UNAVAILABLE,
    HELPER_NOT_FOUND,
)
from mypanel.jobs.context import JobContext
from mypanel.jobs.manager import JobHandler, JobManager
from mypanel.jobs.router import RETRY_BUILDERS
from mypanel.logging import get_logger

docker_router = APIRouter(prefix="/docker", tags=["docker"])

log = get_logger(__name__)

ServiceDep = Annotated[AuthService, Depends(get_auth_service)]

# Keep in sync with mypanel/helper/actions/dockersvc.py (helper re-validates).
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_IMAGE_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,254}$"
_REPOSITORY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$"
_TAG_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"

_HELPER_TIMEOUT_S = 30.0
# stop/restart wait for the engine's kill grace period before returning.
_ACTION_TIMEOUT_S = 60.0
_PULL_TIMEOUT_S = 1800.0
_STREAM_READ_TIMEOUT_S = 15.0

ContainerId = Annotated[str, Path(pattern=_NAME_PATTERN)]
ContainerAction = Literal["start", "stop", "restart", "pause", "unpause", "kill", "remove"]
StreamAuthDep = Annotated[CurrentAuth, Depends(resolve_stream_auth)]


def _helper(holder: Request | WebSocket) -> HelperClient:
    helper: HelperClient = holder.app.state.helper
    return helper


def _jobs(request: Request) -> JobManager:
    jobs: JobManager = request.app.state.jobs
    return jobs


def _map_helper_error(exc: HelperActionError) -> AppError:
    """Semantic status for docker helper failures (503/404/409, else 400)."""
    if exc.helper_code == HELPER_DOCKER_UNAVAILABLE:
        return DockerUnavailableError(exc.detail or "Docker daemon is unavailable.")
    if exc.helper_code == HELPER_NOT_FOUND:
        return NotFoundError(exc.detail or "Docker object not found.")
    if exc.helper_code == HELPER_CONFLICT:
        return ConflictError(exc.detail or "Docker object is in a conflicting state.")
    return exc


async def _docker_call(
    request: Request,
    action: str,
    args: dict[str, Any],
    *,
    timeout_s: float = _HELPER_TIMEOUT_S,
) -> dict[str, Any]:
    try:
        result: dict[str, Any] = await _helper(request).acall(action, args, timeout_s=timeout_s)
    except HelperActionError as exc:
        raise _map_helper_error(exc) from exc
    return result


async def _audited_call(
    request: Request,
    db: AsyncSession,
    auth: CurrentAuth,
    *,
    action: str,
    audit_action: str,
    target: str,
    args: dict[str, Any],
    timeout_s: float = _HELPER_TIMEOUT_S,
) -> dict[str, Any]:
    """Run a mutating helper action, recording an audit row for both outcomes."""
    try:
        result = await _docker_call(request, action, args, timeout_s=timeout_s)
    except AppError as exc:
        await record_audit(
            db,
            resource="docker",
            action=audit_action,
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
        resource="docker",
        action=audit_action,
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return result


# ---------------------------------------------------------------------------
# info / containers
# ---------------------------------------------------------------------------


@docker_router.get("/info")
async def docker_info(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Daemon summary; ``{"available": false}`` when the engine is unreachable."""
    return await _docker_call(request, "docker.info", {})


@docker_router.get("/containers")
async def list_containers(
    request: Request,
    _auth: ActiveSessionDep,
    all: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Container list (running only by default; ``all=true`` includes stopped)."""
    return await _docker_call(request, "docker.container_list", {"all": all})


@docker_router.get("/containers/{container_id}")
async def inspect_container(
    request: Request, container_id: ContainerId, _auth: ActiveSessionDep
) -> dict[str, Any]:
    """Trimmed inspect of one container (by id or name)."""
    return await _docker_call(request, "docker.container_inspect", {"id": container_id})


@docker_router.get("/containers/{container_id}/stats")
async def container_stats(
    request: Request, container_id: ContainerId, _auth: ActiveSessionDep
) -> dict[str, Any]:
    """One-shot resource stats snapshot (CPU%/mem/net/blk/pids) for a container.

    Feeds the detail view's ~2s poll; ``docker.unavailable`` (503) when the
    daemon is down and ``not_found`` (404) for an unknown/removed container.
    """
    return await _docker_call(request, "docker.stats", {"id": container_id})


@docker_router.get("/stats")
async def all_container_stats(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Stats snapshots for all running containers, keyed by full container id.

    Feeds the container list page's ~8s poll. The helper samples containers in
    parallel but each round still blocks ~1s on the daemon, hence the action
    timeout; ``docker.unavailable`` (503) when the daemon is down.
    """
    return await _docker_call(request, "docker.stats_all", {}, timeout_s=_ACTION_TIMEOUT_S)


class ContainerActionRequest(BaseModel):
    force: bool = False


@docker_router.post("/containers/{container_id}/{action}", dependencies=[Depends(require_csrf)])
async def container_action(
    container_id: ContainerId,
    action: ContainerAction,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    service: ServiceDep,
    body: ContainerActionRequest | None = None,
) -> dict[str, Any]:
    """Container lifecycle verb; ``remove`` additionally requires recent re-auth."""
    force = body.force if body is not None else False
    target = f"{container_id}:{action}"
    if action == "remove" and not service.has_recent_reauth(auth.session):
        exc = ReauthRequiredError()
        await record_audit(
            db,
            resource="docker",
            action="docker.container_action",
            result="denied",
            target=target,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise exc
    await _audited_call(
        request,
        db,
        auth,
        action="docker.container_action",
        audit_action="docker.container_action",
        target=target,
        args={"id": container_id, "action": action, "force": force},
        timeout_s=_ACTION_TIMEOUT_S,
    )
    return {"ok": True}


@docker_router.get("/containers/{container_id}/logs")
async def container_logs(
    request: Request,
    container_id: ContainerId,
    _auth: ActiveSessionDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, Any]:
    """Snapshot of the last ``tail`` log lines (frame-budget truncated)."""
    return await _docker_call(
        request, "docker.logs", {"id": container_id, "tail": tail, "follow": False}
    )


def _sse_event(topic: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {topic}\ndata: {payload}\n\n"


async def _pipe_event_source(request: Request, pipe: io.FileIO) -> AsyncIterator[str]:
    """Bridge a helper log pipe to SSE ``log`` events (line-buffered)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
    buffer = b""
    try:
        yield _sse_event("ready", {})
        while True:
            if await request.is_disconnected():
                return
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=_STREAM_READ_TIMEOUT_S)
            except TimeoutError:
                # Heartbeat comment keeps intermediaries from closing the stream.
                yield ": ping\n\n"
                continue
            if not chunk:
                # Helper closed the write end: container gone or follow ended.
                if buffer:
                    yield _sse_event("log", {"line": buffer.decode("utf-8", "replace")})
                yield _sse_event("eof", {})
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                yield _sse_event("log", {"line": line.decode("utf-8", "replace")})
    finally:
        transport.close()


@docker_router.get("/containers/{container_id}/logs/stream")
async def stream_container_logs(
    request: Request,
    container_id: ContainerId,
    _auth: StreamAuthDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> StreamingResponse:
    """Live log follow as SSE, fed by the helper's streaming pipe descriptor."""
    helper = _helper(request)
    try:
        _result, fds = await asyncio.to_thread(
            helper.call_with_fds,
            "docker.logs",
            {"id": container_id, "tail": tail, "follow": True},
            timeout_s=_HELPER_TIMEOUT_S,
        )
    except HelperActionError as exc:
        raise _map_helper_error(exc) from exc
    if not fds:
        raise DockerUnavailableError("Helper did not return a log stream descriptor.")
    # Wrap immediately: the file object guarantees the fd is reclaimed even if
    # the response generator is never started. Extra descriptors are defensive.
    close_fds(fds[1:])
    pipe = os.fdopen(fds[0], "rb", buffering=0)
    return StreamingResponse(
        _pipe_event_source(request, pipe),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# exec WebSocket (§5.3): bridge a hijacked exec socket to a browser terminal
# ---------------------------------------------------------------------------

_WS_REJECT_CODE = 1008  # policy violation (origin/host/session/id)
_WS_INTERNAL_CODE = 1011  # internal error (daemon/container unavailable)
_EXEC_CMD = ["/bin/sh"]
_EXEC_READ_CHUNK = 65536
_EXEC_TIMEOUT_S = 30.0


def _origin_allowed(websocket: WebSocket, settings: Settings) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        return False
    return origin.lower() in {o.lower() for o in settings.allowed_origins()}


def _host_allowed(websocket: WebSocket, settings: Settings) -> bool:
    host = websocket.headers.get("host")
    if host is None:
        return False
    return host.lower() in {h.lower() for h in settings.allowed_hosts()}


async def _resolve_ws_auth(websocket: WebSocket) -> CurrentAuth | None:
    """Validate the session cookie for the WebSocket; None means reject.

    Mirrors :func:`mypanel.auth.deps.resolve_stream_auth` but reads the cookie
    off the WebSocket and requires an ``active`` session (spec §5.3/§5.2).
    """
    raw_token = websocket.cookies.get(COOKIE_NAME)
    if raw_token is None:
        return None
    sessionmaker = websocket.app.state.sessionmaker
    settings: Settings = websocket.app.state.settings
    async with sessionmaker() as db:
        found = await AuthService(db, settings).get_valid_session(raw_token)
    if found is None:
        return None
    session, user = found
    if session.state != SessionState.ACTIVE:
        return None
    return CurrentAuth(session=session, user=user, raw_token=raw_token)


async def _send_ws_error(websocket: WebSocket, exc: AppError) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_json(
            {"type": "error", "code": exc.code, "message": exc.detail or exc.title}
        )


async def _record_exec_audit(
    websocket: WebSocket,
    auth: CurrentAuth,
    container_id: str,
    action: str,
    *,
    result: str = "ok",
    error_code: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Write one exec audit row on its own short-lived session (open/close/denied)."""
    sessionmaker = websocket.app.state.sessionmaker
    async with sessionmaker() as db:
        # record_audit only reads HTTPConnection attributes present on a
        # WebSocket (state/client/headers); the cast keeps mypy happy.
        await record_audit(
            db,
            resource="docker",
            action=action,
            result=result,
            target=container_id,
            actor_user_id=auth.user.id,
            request=cast("Request", websocket),
            error_code=error_code,
            duration_ms=duration_ms,
            commit=True,
        )


@docker_router.websocket("/containers/{container_id}/exec")
async def container_exec(websocket: WebSocket, container_id: str) -> None:
    """Bridge an interactive ``/bin/sh`` exec to the browser after manual auth.

    The Host/Origin/session checks run **before** ``accept`` because the pure
    ASGI Host/Origin middleware only sees ``http`` scopes (spec §0.6). The
    helper's ``docker.exec_create`` hands back a bidirectional hijacked socket
    fd which is bridged to the WebSocket (binary frames = exec I/O, JSON text
    ``{"type":"resize",cols,rows}`` = TTY resize). The fd is always closed.
    """
    settings: Settings = websocket.app.state.settings
    if (
        not _host_allowed(websocket, settings)
        or not _origin_allowed(websocket, settings)
        or re.match(_NAME_PATTERN, container_id) is None
    ):
        await websocket.close(code=_WS_REJECT_CODE)
        return
    auth = await _resolve_ws_auth(websocket)
    if auth is None:
        await websocket.close(code=_WS_REJECT_CODE)
        return

    await websocket.accept()
    helper = _helper(websocket)
    started = datetime.now(tz=UTC)
    try:
        result, fds = await asyncio.to_thread(
            helper.call_with_fds,
            "docker.exec_create",
            {"id": container_id, "cmd": _EXEC_CMD, "tty": True},
            timeout_s=_EXEC_TIMEOUT_S,
        )
    except HelperActionError as exc:
        mapped = _map_helper_error(exc)
        await _send_ws_error(websocket, mapped)
        await _record_exec_audit(
            websocket,
            auth,
            container_id,
            "docker.exec_open",
            result="denied",
            error_code=mapped.code,
        )
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_INTERNAL_CODE)
        return
    except HelperUnavailableError as exc:
        await _send_ws_error(websocket, exc)
        await _record_exec_audit(
            websocket,
            auth,
            container_id,
            "docker.exec_open",
            result="denied",
            error_code=exc.code,
        )
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_INTERNAL_CODE)
        return

    if not fds:
        await _send_ws_error(websocket, DockerUnavailableError("No exec descriptor returned."))
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_INTERNAL_CODE)
        return

    close_fds(fds[1:])  # defensive: only one descriptor is expected
    sock_fd = fds[0]
    exec_id = str(result.get("exec_id", ""))
    await _record_exec_audit(websocket, auth, container_id, "docker.exec_open")
    try:
        await _bridge_exec(websocket, sock_fd, helper, exec_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.warning("docker.exec_bridge_failed", container=container_id[:12])
    finally:
        close_fds([sock_fd])
        with contextlib.suppress(Exception):
            await websocket.close(code=1000)
        duration_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)
        await _record_exec_audit(
            websocket, auth, container_id, "docker.exec_close", duration_ms=duration_ms
        )


async def _bridge_exec(
    websocket: WebSocket, sock_fd: int, helper: HelperClient, exec_id: str
) -> None:
    """Pump bytes both ways between the WebSocket and the exec socket fd."""
    loop = asyncio.get_running_loop()
    os.set_blocking(sock_fd, False)
    to_ws: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(sock_fd, _EXEC_READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        to_ws.put_nowait(data if data else None)

    loop.add_reader(sock_fd, _on_readable)
    tasks = [
        asyncio.create_task(_exec_sock_to_ws(websocket, to_ws)),
        asyncio.create_task(_exec_ws_to_sock(websocket, sock_fd, helper, exec_id)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        loop.remove_reader(sock_fd)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def _exec_sock_to_ws(websocket: WebSocket, to_ws: asyncio.Queue[bytes | None]) -> None:
    while True:
        data = await to_ws.get()
        if data is None:
            return  # EOF: the exec'd process exited
        await websocket.send_bytes(data)


async def _exec_ws_to_sock(
    websocket: WebSocket, sock_fd: int, helper: HelperClient, exec_id: str
) -> None:
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect:
            return
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            await _exec_write_all(sock_fd, data)
            continue
        text = message.get("text")
        if text is not None:
            await _handle_exec_control(text, helper, exec_id)


async def _handle_exec_control(text: str, helper: HelperClient, exec_id: str) -> None:
    """Handle a JSON control frame; only ``{"type":"resize",cols,rows}`` is acted on."""
    try:
        message = json.loads(text)
    except ValueError:
        return
    if not isinstance(message, dict) or message.get("type") != "resize" or not exec_id:
        return
    cols = message.get("cols")
    rows = message.get("rows")
    if not (isinstance(cols, int) and isinstance(rows, int)):
        return
    with contextlib.suppress(HelperActionError, HelperUnavailableError):
        await helper.acall(
            "docker.exec_resize",
            {"exec_id": exec_id, "height": rows, "width": cols},
            timeout_s=_EXEC_TIMEOUT_S,
        )


async def _exec_write_all(sock_fd: int, data: bytes) -> None:
    loop = asyncio.get_running_loop()
    view = memoryview(data)
    while view:
        try:
            written = os.write(sock_fd, view)
        except BlockingIOError:
            await _wait_writable(loop, sock_fd)
            continue
        except OSError:
            return
        view = view[written:]


async def _wait_writable(loop: asyncio.AbstractEventLoop, fd: int) -> None:
    future: asyncio.Future[None] = loop.create_future()

    def _on_writable() -> None:
        loop.remove_writer(fd)
        if not future.done():
            future.set_result(None)

    loop.add_writer(fd, _on_writable)
    try:
        await future
    finally:
        loop.remove_writer(fd)


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


@docker_router.get("/images")
async def list_images(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Image list."""
    return await _docker_call(request, "docker.image_list", {})


class ImagePullRequest(BaseModel):
    repository: str = Field(min_length=1, max_length=255, pattern=_REPOSITORY_PATTERN)
    tag: str = Field(default="latest", min_length=1, max_length=128, pattern=_TAG_PATTERN)


def _make_pull_handler(helper: HelperClient, repository: str, tag: str) -> JobHandler:
    """Job handler pulling one image through the helper (blocking on its side)."""

    async def handler(ctx: JobContext) -> dict[str, Any] | None:
        ref = f"{repository}:{tag}"
        await ctx.log(f"开始拉取镜像 {ref}")
        await ctx.progress(None, stage="pulling")
        try:
            result = await helper.acall(
                "docker.image_pull",
                {"repository": repository, "tag": tag},
                timeout_s=_PULL_TIMEOUT_S,
            )
        except HelperActionError as exc:
            await ctx.log(f"拉取失败:{exc.detail or exc.title}", level="error")
            raise
        await ctx.progress(100, stage="done")
        await ctx.log(f"拉取完成:{result.get('id', '')}(共 {result.get('layers', 0)} 层)")
        return result

    return handler


async def _submit_pull(request: Request, repository: str, tag: str) -> str:
    return await _jobs(request).submit(
        type="docker.image_pull",
        title=f"拉取镜像 {repository}:{tag}",
        handler=_make_pull_handler(_helper(request), repository, tag),
        params={"repository": repository, "tag": tag},
        resource_key=f"docker-image:{repository}:{tag}",
        request_id=getattr(request.state, "request_id", ""),
    )


@docker_router.post("/images/pull", status_code=202, dependencies=[Depends(require_csrf)])
async def pull_image(
    body: ImagePullRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, str]:
    """Start an image pull as a background job; returns ``{"job_id": ...}``."""
    target = f"{body.repository}:{body.tag}"
    try:
        job_id = await _submit_pull(request, body.repository, body.tag)
    except AppError as exc:
        await record_audit(
            db,
            resource="docker",
            action="docker.image_pull",
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
        resource="docker",
        action="docker.image_pull",
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"job_id": job_id}


async def _retry_image_pull(request: Request, job: dict[str, Any]) -> str:
    """Rebuild an image-pull job from its stored params (``/jobs/{id}/retry``)."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as db:
        row = await db.get(Job, str(job["id"]))
        raw = row.params if row is not None else None
    params: dict[str, Any] = json.loads(raw) if raw else {}
    repository = str(params.get("repository", ""))
    tag = str(params.get("tag", "latest"))
    if not repository:
        raise ConflictError("Job parameters are missing; cannot retry.")
    return await _submit_pull(request, repository, tag)


RETRY_BUILDERS["docker.image_pull"] = _retry_image_pull


@docker_router.delete("/images/{image_ref:path}", dependencies=[Depends(require_csrf)])
async def remove_image(
    image_ref: Annotated[str, Path(pattern=_IMAGE_REF_PATTERN)],
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Remove an image by id or ``repo:tag`` reference."""
    await _audited_call(
        request,
        db,
        auth,
        action="docker.image_remove",
        audit_action="docker.image_remove",
        target=image_ref,
        args={"id": image_ref, "force": force},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# networks
# ---------------------------------------------------------------------------


@docker_router.get("/networks")
async def list_networks(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Network list."""
    return await _docker_call(request, "docker.network_list", {})


class NetworkCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=_NAME_PATTERN)
    driver: Literal["bridge", "overlay", "macvlan", "ipvlan"] = "bridge"
    internal: bool = False


@docker_router.post("/networks", dependencies=[Depends(require_csrf)])
async def create_network(
    body: NetworkCreateRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Create a user-defined network."""
    return await _audited_call(
        request,
        db,
        auth,
        action="docker.network_create",
        audit_action="docker.network_create",
        target=body.name,
        args={"name": body.name, "driver": body.driver, "internal": body.internal},
    )


@docker_router.delete("/networks/{network_id}", dependencies=[Depends(require_csrf)])
async def remove_network(
    network_id: Annotated[str, Path(pattern=_NAME_PATTERN)],
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Remove a network by id or name."""
    await _audited_call(
        request,
        db,
        auth,
        action="docker.network_remove",
        audit_action="docker.network_remove",
        target=network_id,
        args={"id": network_id},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# volumes
# ---------------------------------------------------------------------------


@docker_router.get("/volumes")
async def list_volumes(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Volume list."""
    return await _docker_call(request, "docker.volume_list", {})


class VolumeCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=_NAME_PATTERN)


@docker_router.post("/volumes", dependencies=[Depends(require_csrf)])
async def create_volume(
    body: VolumeCreateRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Create a named volume."""
    return await _audited_call(
        request,
        db,
        auth,
        action="docker.volume_create",
        audit_action="docker.volume_create",
        target=body.name,
        args={"name": body.name},
    )


@docker_router.delete("/volumes/{volume_name}", dependencies=[Depends(require_csrf)])
async def remove_volume(
    volume_name: Annotated[str, Path(pattern=_NAME_PATTERN)],
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Remove a volume (409 while a container still uses it)."""
    await _audited_call(
        request,
        db,
        auth,
        action="docker.volume_remove",
        audit_action="docker.volume_remove",
        target=volume_name,
        args={"name": volume_name, "force": force},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# daemon configuration (/etc/docker/daemon.json)
# ---------------------------------------------------------------------------

#: systemd unit that owns the daemon; restarting it applies daemon.json.
_DOCKER_UNIT = "docker.service"
_DAEMON_CONFIG_MAX_BYTES = 256 * 1024
_RESTART_TIMEOUT_S = 120.0


class DaemonConfigRequest(BaseModel):
    """Body for a full daemon.json replacement (validated as JSON by the helper)."""

    content: str = Field(max_length=_DAEMON_CONFIG_MAX_BYTES)


async def _daemon_started_at(request: Request) -> float:
    """Epoch seconds when the running dockerd started, or ``0.0`` if unknown.

    Taken from the unit's MainPID rather than systemd's timestamp string: the
    PID's creation time is unambiguous and needs no locale-dependent parsing.
    """
    try:
        status = await _docker_call(request, "systemd.status", {"unit": _DOCKER_UNIT})
    except AppError:
        return 0.0
    try:
        pid = int(status.get("main_pid") or 0)
        if pid <= 0:
            return 0.0
        return float(psutil.Process(pid).create_time())
    except (ValueError, TypeError, psutil.Error):
        return 0.0


@docker_router.get("/daemon-config")
async def get_daemon_config(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Read daemon.json plus enough context to tell whether a restart is due.

    ``parsed`` is the decoded object so the UI can drive a structured form
    without re-implementing JSON parsing; it is ``{}`` when the file is absent
    or unparseable (``parse_error`` then carries the reason and the raw text is
    still returned for the editor).
    """
    result = await _docker_call(request, "dockercfg.read", {})
    content = str(result.get("content", ""))
    parsed: dict[str, Any] = {}
    parse_error = ""
    if content.strip():
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            parse_error = f"第 {exc.lineno} 行 {exc.msg}"
        else:
            if isinstance(decoded, dict):
                parsed = decoded
            else:
                parse_error = "顶层不是 JSON 对象"
    mtime = float(result.get("mtime") or 0.0)
    started = await _daemon_started_at(request)
    return {
        "path": result.get("path", ""),
        "exists": bool(result.get("exists")),
        "content": content,
        "parsed": parsed,
        "parse_error": parse_error,
        "mtime": mtime,
        # Config newer than the running daemon => the edits are not live yet.
        "restart_pending": bool(mtime and started and mtime > started),
    }


@docker_router.put(
    "/daemon-config",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def put_daemon_config(
    body: DaemonConfigRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Validate, back up and write daemon.json (CSRF + recent reauth).

    Writing alone changes nothing at runtime — the caller must restart the
    daemon to apply it, which is a separate, explicitly confirmed step.
    """
    result = await _audited_call(
        request,
        db,
        auth,
        action="dockercfg.write",
        audit_action="docker.daemon_config_write",
        target="/etc/docker/daemon.json",
        args={"content": body.content},
    )
    return {
        "ok": True,
        "path": result.get("path", ""),
        "content": result.get("content", ""),
        "backup": result.get("backup", ""),
    }


@docker_router.post(
    "/daemon-restart",
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def restart_daemon(request: Request, auth: ActiveSessionDep, db: DbDep) -> dict[str, Any]:
    """Restart dockerd so a new daemon.json takes effect (CSRF + recent reauth).

    Unless ``live-restore`` is enabled this stops and restarts every running
    container, so the UI states how many are affected before asking.
    """
    await _audited_call(
        request,
        db,
        auth,
        action="systemd.restart",
        audit_action="docker.daemon_restart",
        target=_DOCKER_UNIT,
        args={"unit": _DOCKER_UNIT},
        timeout_s=_RESTART_TIMEOUT_S,
    )
    return {"ok": True}
