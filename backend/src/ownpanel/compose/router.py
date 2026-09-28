"""Compose project API: discovery/import, config & ps, file editing, lifecycle jobs (§4.6).

Project paths must be absolute, normalised and strictly inside
``settings.docker.compose_root``; the check runs API-side on every operation
(the helper knows no settings). Compose file reads/writes reuse the ``files.*``
helper actions (staged write + atomic install with optimistic etags). The
``up``/``down``/``restart`` verbs run as background jobs whose handlers stream
the helper's subprocess output into the job log; jobs of one project are
serialised via ``resource_key="compose:<path>"``. Response field names are
frontend contract; do not rename them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownpanel.audit import record_audit
from ownpanel.auth.deps import (
    ActiveSessionDep,
    DbDep,
    SettingsDep,
    require_csrf,
    require_recent_reauth,
)
from ownpanel.config import Settings
from ownpanel.db.models import ComposeProject, Job
from ownpanel.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    HelperActionError,
    HelperUnavailableError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationFailedError,
)
from ownpanel.helper.actions.composesvc import COMPOSE_FILE_NAMES, EXIT_MARKER
from ownpanel.ipc.client import HelperClient
from ownpanel.ipc.fdpass import close_fds
from ownpanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_DOCKER_UNAVAILABLE,
    HELPER_NOT_FOUND,
    HELPER_PATH_NOT_FOUND,
)
from ownpanel.jobs.context import JobContext
from ownpanel.jobs.manager import JobHandler, JobManager
from ownpanel.jobs.router import RETRY_BUILDERS

compose_router = APIRouter(prefix="/compose", tags=["compose"])

# Mirrors docker compose project-name rules closely enough for URL/table safety;
# a leading alphanumeric also rules out "." / ".." path tricks.
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_NAME_RE = re.compile(_NAME_PATTERN)

NameParam = Annotated[str, Path(pattern=_NAME_PATTERN)]

ComposeVerb = Literal["up", "down", "restart"]

_VERB_TITLES: dict[str, str] = {"up": "启动", "down": "停止", "restart": "重启"}

_FILE_MAX_BYTES = 1024 * 1024
_HELPER_TIMEOUT_S = 15.0
_CONFIG_TIMEOUT_S = 90.0
_READ_CHUNK = 64 * 1024


class ComposeImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=_NAME_PATTERN)
    path: str = Field(min_length=1, max_length=4096)


class ComposeFileWriteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=_FILE_MAX_BYTES)
    # Keep the Literal in sync with composesvc.COMPOSE_FILE_NAMES.
    file: Literal["compose.yaml", "compose.yml", "docker-compose.yml"] | None = None
    expected_etag: str | None = Field(default=None, max_length=128)


def _helper(request: Request) -> HelperClient:
    helper: HelperClient = request.app.state.helper
    return helper


def _jobs(request: Request) -> JobManager:
    jobs: JobManager = request.app.state.jobs
    return jobs


def _compose_root(settings: Settings) -> str:
    return os.path.normpath(str(settings.docker.compose_root))


def _validate_project_path(path: str, settings: Settings) -> str:
    """Absolute, normalised and strictly inside the compose root.

    Raises:
        ValidationFailedError: Malformed path (relative, ``..``, ``//``, NUL...).
        ForbiddenError: Well-formed but escaping ``settings.docker.compose_root``.
    """
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\0" in path
        or os.path.normpath(path) != path
    ):
        raise ValidationFailedError(f"invalid project path: {path!r}")
    root = _compose_root(settings)
    prefix = root if root.endswith("/") else root + "/"
    if path == root or not path.startswith(prefix):
        raise ForbiddenError("project path must be inside the compose root")
    return path


async def _discover(request: Request, settings: Settings) -> list[dict[str, Any]]:
    """Helper-side one-level scan of the compose root."""
    result = await _helper(request).acall(
        "compose.discover", {"root": _compose_root(settings)}, timeout_s=_HELPER_TIMEOUT_S
    )
    projects = result.get("projects")
    if not isinstance(projects, list):
        return []
    return [item for item in projects if isinstance(item, dict)]


async def _resolve_project(
    request: Request, db: AsyncSession, settings: Settings, name: str
) -> dict[str, Any]:
    """Project by name: imported row first, then a discovered directory.

    The resolved path is (re-)validated against the current compose root, so an
    imported row pointing outside a changed root is refused rather than used.
    """
    row = await db.scalar(select(ComposeProject).where(ComposeProject.name == name))
    if row is not None:
        return {
            "name": row.name,
            "path": _validate_project_path(row.path, settings),
            "imported": True,
        }
    for item in await _discover(request, settings):
        if item.get("name") == name:
            return {
                "name": name,
                "path": _validate_project_path(str(item.get("path", "")), settings),
                "imported": False,
            }
    raise NotFoundError(f"compose project not found: {name}")


# ---------------------------------------------------------------------------
# Listing / import / delete
# ---------------------------------------------------------------------------


@compose_router.get("/projects")
async def list_projects(
    request: Request, _auth: ActiveSessionDep, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """Imported projects (DB) merged with directories discovered under the root."""
    rows = (await db.scalars(select(ComposeProject).order_by(ComposeProject.name))).all()
    discovered = await _discover(request, settings)
    by_path = {str(item.get("path", "")): item for item in discovered}
    projects: list[dict[str, Any]] = []
    imported_paths = set()
    for row in rows:
        found = by_path.get(row.path)
        imported_paths.add(row.path)
        projects.append(
            {
                "name": row.name,
                "path": row.path,
                "imported": True,
                "has_compose_file": bool(found and found.get("has_compose_file")),
                "compose_file": found.get("compose_file") if found else None,
                "created_at": row.created_at.isoformat(),
            }
        )
    for item in discovered:
        if str(item.get("path", "")) in imported_paths:
            continue
        projects.append(
            {
                "name": str(item.get("name", "")),
                "path": str(item.get("path", "")),
                "imported": False,
                "has_compose_file": bool(item.get("has_compose_file")),
                "compose_file": item.get("compose_file"),
                "created_at": None,
            }
        )
    projects.sort(key=lambda item: str(item["name"]))
    return {"projects": projects, "compose_root": _compose_root(settings)}


@compose_router.post("/projects", dependencies=[Depends(require_csrf)])
async def import_project(
    body: ComposeImportRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Register an existing directory under the compose root as a named project."""
    target = f"{body.name}:{body.path}"
    try:
        path = _validate_project_path(body.path, settings)
        try:
            st = await _helper(request).acall(
                "files.stat", {"path": path}, timeout_s=_HELPER_TIMEOUT_S
            )
        except HelperActionError as exc:
            if exc.helper_code == HELPER_PATH_NOT_FOUND:
                raise NotFoundError(f"project directory not found: {path}") from exc
            raise
        if st.get("type") != "dir":
            raise ValidationFailedError("project path is not a directory")
        duplicate = await db.scalar(
            select(ComposeProject).where(
                (ComposeProject.name == body.name) | (ComposeProject.path == path)
            )
        )
        if duplicate is not None:
            raise ConflictError("project name or path is already imported")
        now = datetime.now(tz=UTC)
        db.add(ComposeProject(name=body.name, path=path, created_at=now, updated_at=now))
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise ConflictError("project name or path is already imported") from exc
    except AppError as exc:
        await record_audit(
            db,
            resource="compose",
            action="compose.import",
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
        resource="compose",
        action="compose.import",
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"name": body.name, "path": path, "imported": True}


@compose_router.delete(
    "/projects/{name}",
    status_code=204,
    dependencies=[Depends(require_csrf), Depends(require_recent_reauth)],
)
async def delete_project(
    name: NameParam, request: Request, auth: ActiveSessionDep, db: DbDep
) -> Response:
    """Forget an imported project (the directory and containers are untouched)."""
    try:
        row = await db.scalar(select(ComposeProject).where(ComposeProject.name == name))
        if row is None:
            raise NotFoundError(f"compose project not imported: {name}")
        await db.delete(row)
        await db.commit()
    except AppError as exc:
        await record_audit(
            db,
            resource="compose",
            action="compose.delete",
            result="denied",
            target=name,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="compose",
        action="compose.delete",
        target=name,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Detail (config + ps)
# ---------------------------------------------------------------------------


@compose_router.get("/projects/{name}")
async def project_detail(
    name: NameParam,
    request: Request,
    _auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Project detail: compose-file validity, declared services and runtime state.

    Degrades gracefully: a missing compose file or an unreachable docker CLI /
    daemon is reported through ``has_compose_file`` / ``docker_available``
    instead of failing the whole request.
    """
    project = await _resolve_project(request, db, settings, name)
    detail: dict[str, Any] = {
        **project,
        "has_compose_file": False,
        "compose_file": None,
        "config_valid": None,
        "config_checked_by": None,
        "config_error": None,
        "services": [],
        "ps": [],
        "docker_available": True,
    }
    helper = _helper(request)
    try:
        config = await helper.acall(
            "compose.config", {"path": project["path"]}, timeout_s=_CONFIG_TIMEOUT_S
        )
    except HelperActionError as exc:
        if exc.helper_code == HELPER_NOT_FOUND:
            return detail
        raise
    detail.update(
        has_compose_file=True,
        compose_file=config.get("compose_file"),
        config_valid=bool(config.get("valid")),
        config_checked_by=config.get("checked_by"),
        config_error=config.get("error"),
        services=config.get("services", []),
    )
    try:
        ps = await helper.acall(
            "compose.ps", {"path": project["path"]}, timeout_s=_HELPER_TIMEOUT_S
        )
        detail["ps"] = ps.get("services", [])
    except HelperActionError as exc:
        if exc.helper_code != HELPER_DOCKER_UNAVAILABLE:
            raise
        detail["docker_available"] = False
    return detail


# ---------------------------------------------------------------------------
# Compose file read / write (via the files.* helper actions)
# ---------------------------------------------------------------------------


def _read_fd_bounded(fd: int, cap: int) -> str:
    """Drain a read descriptor up to ``cap`` bytes (blocking; run in a thread)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, _READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise PayloadTooLargeError(f"compose file exceeds {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _write_fd_all(fd: int, data: bytes) -> None:
    """Write the full payload into a descriptor (blocking; run in a thread)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


async def _read_project_file(request: Request, path: str) -> tuple[str, str, str]:
    """(file name, content, etag) of the project's compose file; 404 when absent."""
    helper = _helper(request)
    for candidate in COMPOSE_FILE_NAMES:
        target = os.path.join(path, candidate)
        try:
            result, fds = await asyncio.to_thread(
                helper.call_with_fds,
                "files.read_fd",
                {"path": target},
                timeout_s=_HELPER_TIMEOUT_S,
            )
        except HelperActionError as exc:
            if exc.helper_code == HELPER_PATH_NOT_FOUND:
                continue
            raise
        try:
            if not fds:
                raise HelperUnavailableError("helper returned no read descriptor")
            if int(result.get("size", 0)) > _FILE_MAX_BYTES:
                raise PayloadTooLargeError(f"compose file exceeds {_FILE_MAX_BYTES} bytes")
            content = await asyncio.to_thread(_read_fd_bounded, fds[0], _FILE_MAX_BYTES)
        finally:
            close_fds(fds)
        return candidate, content, str(result.get("etag", ""))
    raise NotFoundError("no compose file in project")


async def _detect_file_name(request: Request, path: str) -> str | None:
    """Existing compose file name of a project, via ``files.stat``; None when none."""
    helper = _helper(request)
    for candidate in COMPOSE_FILE_NAMES:
        try:
            st = await helper.acall(
                "files.stat", {"path": os.path.join(path, candidate)}, timeout_s=_HELPER_TIMEOUT_S
            )
        except HelperActionError as exc:
            if exc.helper_code == HELPER_PATH_NOT_FOUND:
                continue
            raise
        if st.get("type") == "file":
            return candidate
    return None


def _validate_compose_yaml(content: str) -> None:
    """The editable contract: YAML mapping with a non-empty ``services`` mapping.

    Raises:
        ValidationFailedError: Parse failure or wrong document shape.
    """
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValidationFailedError(f"invalid YAML: {exc}") from None
    if (
        not isinstance(doc, dict)
        or not isinstance(doc.get("services"), dict)
        or not doc["services"]
    ):
        raise ValidationFailedError(
            "compose file must be a YAML mapping with a non-empty 'services' mapping"
        )


@compose_router.get("/projects/{name}/file")
async def read_project_file(
    name: NameParam,
    request: Request,
    _auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Raw compose file content with its optimistic-concurrency etag."""
    project = await _resolve_project(request, db, settings, name)
    file_name, content, etag = await _read_project_file(request, project["path"])
    return {
        "name": project["name"],
        "path": project["path"],
        "file": file_name,
        "content": content,
        "etag": etag,
    }


@compose_router.put("/projects/{name}/file", dependencies=[Depends(require_csrf)])
async def write_project_file(
    name: NameParam,
    body: ComposeFileWriteRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Validate and atomically replace the compose file (staged write + install)."""
    helper = _helper(request)
    file_name = "compose.yaml"
    try:
        project = await _resolve_project(request, db, settings, name)
        _validate_compose_yaml(body.content)
        data = body.content.encode("utf-8")
        if len(data) > _FILE_MAX_BYTES:
            raise PayloadTooLargeError(f"compose file exceeds {_FILE_MAX_BYTES} bytes")
        file_name = body.file or await _detect_file_name(request, project["path"]) or file_name
        target_path = os.path.join(project["path"], file_name)
        staged_result, fds = await asyncio.to_thread(
            helper.call_with_fds,
            "files.open_write",
            {"dir": project["path"]},
            timeout_s=_HELPER_TIMEOUT_S,
        )
        staged_path = str(staged_result.get("staged_path", ""))
        try:
            if not fds:
                raise HelperUnavailableError("helper returned no write descriptor")
            await asyncio.to_thread(_write_fd_all, fds[0], data)
        except BaseException:
            close_fds(fds)
            with contextlib.suppress(AppError):
                await helper.acall(
                    "files.delete",
                    {"path": staged_path, "recursive": False},
                    timeout_s=_HELPER_TIMEOUT_S,
                )
            raise
        close_fds(fds)
        try:
            installed = await helper.acall(
                "files.install",
                {
                    "staged_path": staged_path,
                    "target": target_path,
                    "expected_etag": body.expected_etag,
                    "mode": None,
                },
                timeout_s=_HELPER_TIMEOUT_S,
            )
        except HelperActionError as exc:
            if exc.helper_code == HELPER_CONFLICT:
                raise ConflictError("compose file changed on disk; reload and retry") from exc
            raise
    except AppError as exc:
        await record_audit(
            db,
            resource="compose",
            action="compose.write_file",
            result="denied",
            target=name,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="compose",
        action="compose.write_file",
        target=f"{name}:{file_name}",
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"file": file_name, "etag": str(installed.get("etag", ""))}


# ---------------------------------------------------------------------------
# up / down / restart as background jobs
# ---------------------------------------------------------------------------


def _make_compose_handler(helper: HelperClient, verb: str, path: str) -> JobHandler:
    """Job handler: run the compose verb via the helper, stream output to the log."""

    async def handler(ctx: JobContext) -> dict[str, Any] | None:
        await ctx.log(f"开始执行 docker compose {verb}: {path}")
        await ctx.progress(None, stage=verb)
        result, fds = await asyncio.to_thread(
            helper.call_with_fds, f"compose.{verb}", {"path": path}, timeout_s=_HELPER_TIMEOUT_S
        )
        if not fds:
            raise RuntimeError("helper 未返回输出流")
        try:
            stream = os.fdopen(fds[0], "r", encoding="utf-8", errors="replace")
        except BaseException:
            close_fds(fds)
            raise
        close_fds(fds[1:])
        exit_code: int | None = None
        try:
            while True:
                line = await asyncio.to_thread(stream.readline)
                if line == "":
                    break
                text = line.rstrip("\n")
                if text.startswith(EXIT_MARKER):
                    raw = text[len(EXIT_MARKER) :].strip()
                    exit_code = int(raw) if raw.lstrip("-").isdigit() else -1
                    continue
                if text:
                    await ctx.log(text)
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()
        await ctx.log(f"docker compose {verb} 子进程已退出, pid {result.get('pid', '?')}")
        if exit_code is None:
            raise RuntimeError(f"docker compose {verb} 输出流意外中断")
        if exit_code != 0:
            raise RuntimeError(f"docker compose {verb} 失败, 退出码 {exit_code}")
        await ctx.progress(100, stage="done")
        return {"exit_code": exit_code}

    return handler


async def _submit_compose_job(request: Request, verb: str, name: str, path: str) -> str:
    """Queue a compose verb; one project's jobs are serialised by resource key."""
    return await _jobs(request).submit(
        type=f"compose.{verb}",
        title=f"{_VERB_TITLES.get(verb, verb)} Compose 项目 {name}",
        handler=_make_compose_handler(_helper(request), verb, path),
        params={"verb": verb, "name": name, "path": path},
        resource_key=f"compose:{path}",
        request_id=getattr(request.state, "request_id", ""),
    )


@compose_router.post(
    "/projects/{name}/{action}", status_code=202, dependencies=[Depends(require_csrf)]
)
async def project_action(
    name: NameParam,
    action: ComposeVerb,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, str]:
    """Start ``docker compose up/down/restart`` as a job; returns ``{"job_id"}``."""
    try:
        project = await _resolve_project(request, db, settings, name)
        job_id = await _submit_compose_job(request, action, project["name"], project["path"])
    except AppError as exc:
        await record_audit(
            db,
            resource="compose",
            action=f"compose.{action}",
            result="denied",
            target=name,
            actor_user_id=auth.user.id,
            request=request,
            error_code=exc.code,
            commit=True,
        )
        raise
    await record_audit(
        db,
        resource="compose",
        action=f"compose.{action}",
        target=name,
        actor_user_id=auth.user.id,
        request=request,
        commit=True,
    )
    return {"job_id": job_id}


async def _retry_compose(request: Request, job: dict[str, Any]) -> str:
    """Rebuild a compose verb job from its stored params (``/jobs/{id}/retry``)."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as db:
        row = await db.get(Job, str(job["id"]))
        raw = row.params if row is not None else None
    params: dict[str, Any] = json.loads(raw) if raw else {}
    verb = str(params.get("verb", ""))
    name = str(params.get("name", ""))
    path = str(params.get("path", ""))
    if verb not in _VERB_TITLES or not name or not path or not _NAME_RE.fullmatch(name):
        raise ConflictError("Job parameters are missing; cannot retry.")
    settings: Settings = request.app.state.settings
    _validate_project_path(path, settings)
    return await _submit_compose_job(request, verb, name, path)


for _verb in ("up", "down", "restart"):
    RETRY_BUILDERS[f"compose.{_verb}"] = _retry_compose
