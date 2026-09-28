"""Files API: browse, download, edit, upload and manage files via the root helper.

Reads proxy ``files.stat``/``files.list``/``files.read_fd``; directory
downloads are streamed zip archives assembled in-process from those same read
actions (no helper changes, no temp files). Writes go through the atomic
``files.open_write`` + ``files.install`` staging flow (optimistic etag
concurrency). Long operations (archive, extract, recursive copy) run in the
job manager and answer ``202 {"job_id"}``. Response field names are frontend
contract; do not rename them.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import itertools
import os
import re
import stat
import time
import urllib.parse
import zipfile
from collections import deque
from collections.abc import AsyncIterator
from typing import IO, Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from mypanel.audit import record_audit
from mypanel.auth.deps import (
    ActiveSessionDep,
    CurrentAuth,
    DbDep,
    SettingsDep,
    require_csrf,
    require_recent_reauth,
    resolve_stream_auth,
)
from mypanel.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    HelperActionError,
    HelperUnavailableError,
    NotFoundError,
    PayloadTooLargeError,
)
from mypanel.ipc.fdpass import close_fds
from mypanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_NOT_FOUND,
    HELPER_PATH_NOT_FOUND,
    HELPER_TOO_LARGE,
)
from mypanel.jobs.context import JobContext

files_router = APIRouter(prefix="/files", tags=["files"])

_PATH_MAX = 4096
# Editor/preview cap: /files/content refuses bigger files with HTTP 413.
_CONTENT_MAX_BYTES = 2 * 1024 * 1024
_CHUNK_BYTES = 256 * 1024
_HELPER_TIMEOUT_S = 10.0
_MUTATE_TIMEOUT_S = 120.0
_JOB_TIMEOUT_S = 3600.0
# Multipart framing overhead allowance for the early Content-Length rejection.
_UPLOAD_CL_SLACK = 64 * 1024

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

# ``?inline=1`` preview whitelist. Responses carry X-Content-Type-Options:
# nosniff, so the browser only renders what we explicitly type here. Images
# only, and deliberately no SVG: image/svg+xml is active content (scripts run
# when the URL is navigated to directly) and would be a stored XSS on the
# panel origin. Everything outside the map falls back to an
# application/octet-stream attachment.
_INLINE_IMAGE_MIME: dict[str, str] = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Directory-zip streaming: recursion depth cap (guards against bind-mount
# cycles — symlinks are never followed) and kernel pseudo-filesystems that
# must not end up inside a download of "/".
_ZIP_MAX_DEPTH = 128
_ZIP_PRUNE_ROOTS = frozenset({"/proc", "/sys", "/dev"})
# zip timestamps live in 1980..2107; clamp so ZipInfo never rejects an mtime.
_ZIP_TIME_MIN = 315532800.0
_ZIP_TIME_MAX = 4102444799.0
# Members at or above this size get an unconditional zip64 header (the stream
# is unseekable, so the choice cannot be revised once the header is out).
_ZIP64_THRESHOLD = 0xFFFF0000
# Multi-entry download cap: keeps the query string well inside proxy URL limits
# and bounds the per-request stat fan-out. Bigger selections belong in the
# /files/archive background job.
_DOWNLOAD_ZIP_MAX_ITEMS = 100


class _BodyTooLargeError(Exception):
    """Raised by the wrapped ASGI receive once the body exceeds the cap."""


def _limit_request_body(request: Request, max_bytes: int) -> None:
    """Wrap ``request``'s ASGI receive to abort once the body exceeds ``max_bytes``.

    This bounds ``request.form()`` (and any stream read) regardless of whether a
    Content-Length header was sent, so a chunked upload cannot spool an
    unbounded file part to disk before the size check runs.
    """
    original = request._receive
    total = 0

    async def receive() -> Any:
        nonlocal total
        message = await original()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > max_bytes:
                raise _BodyTooLargeError()
        return message

    request._receive = receive


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------


class WriteContentRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_PATH_MAX)
    content: str
    expected_etag: str | None = Field(default=None, max_length=64)


class MkdirRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_PATH_MAX)
    mode: int = Field(default=0o755, ge=0, le=0o777)
    parents: bool = False


class RenameRequest(BaseModel):
    src: str = Field(min_length=1, max_length=_PATH_MAX)
    dst: str = Field(min_length=1, max_length=_PATH_MAX)


class CopyRequest(BaseModel):
    src: str = Field(min_length=1, max_length=_PATH_MAX)
    dst: str = Field(min_length=1, max_length=_PATH_MAX)
    recursive: bool = False


class ChmodRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_PATH_MAX)
    mode: int = Field(ge=0, le=0o7777)


class ChownRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_PATH_MAX)
    uid: int = Field(default=-1, ge=-1)
    gid: int = Field(default=-1, ge=-1)


class DeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_PATH_MAX)
    recursive: bool = False


class ArchiveRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)
    dst: str = Field(min_length=1, max_length=_PATH_MAX)
    format: Literal["zip", "tar", "tar.gz"]


class ExtractRequest(BaseModel):
    archive: str = Field(min_length=1, max_length=_PATH_MAX)
    dst_dir: str = Field(min_length=1, max_length=_PATH_MAX)
    format: Literal["zip", "tar", "tar.gz"]


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _semantic(exc: HelperActionError) -> AppError:
    """Give helper failures a meaningful HTTP status where one exists."""
    if exc.helper_code in (HELPER_PATH_NOT_FOUND, HELPER_NOT_FOUND):
        return NotFoundError(exc.detail or "path not found")
    if exc.helper_code == HELPER_CONFLICT:
        return ConflictError(exc.detail or "conflicting filesystem state")
    if exc.helper_code == HELPER_TOO_LARGE:
        return PayloadTooLargeError(exc.detail or "content too large")
    return exc


async def _files_call(
    request: Request,
    action: str,
    args: dict[str, Any],
    *,
    timeout_s: float = _HELPER_TIMEOUT_S,
) -> dict[str, Any]:
    try:
        result: dict[str, Any] = await request.app.state.helper.acall(
            action, args, timeout_s=timeout_s
        )
    except HelperActionError as exc:
        raise _semantic(exc) from exc
    return result


async def _audit(
    db: AsyncSession,
    request: Request,
    auth: CurrentAuth,
    action: str,
    target: str,
    *,
    error: AppError | None = None,
) -> None:
    await record_audit(
        db,
        resource="files",
        action=action,
        result="denied" if error is not None else "ok",
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        error_code=error.code if error is not None else None,
        commit=True,
    )


async def _audited_call(
    request: Request,
    db: AsyncSession,
    auth: CurrentAuth,
    *,
    action: str,
    helper_action: str,
    args: dict[str, Any],
    target: str,
    timeout_s: float = _MUTATE_TIMEOUT_S,
) -> dict[str, Any]:
    """Run a mutating helper action with the standard success/denied audit rows."""
    try:
        result = await _files_call(request, helper_action, args, timeout_s=timeout_s)
    except AppError as exc:
        await _audit(db, request, auth, action, target, error=exc)
        raise
    await _audit(db, request, auth, action, target)
    return result


async def _submit_helper_job(
    request: Request,
    *,
    type_: str,
    title: str,
    helper_action: str,
    args: dict[str, Any],
    resource_key: str,
) -> str:
    """Queue a job whose body is one long-running helper call."""
    helper = request.app.state.helper

    async def run(ctx: JobContext) -> dict[str, Any] | None:
        await ctx.log(f"{helper_action} started")
        result: dict[str, Any] = await helper.acall(helper_action, args, timeout_s=_JOB_TIMEOUT_S)
        await ctx.log(f"{helper_action} finished")
        return result

    job_id: str = await request.app.state.jobs.submit(
        type=type_,
        title=title,
        handler=run,
        params={"action": helper_action, "args": args},
        resource_key=resource_key,
        request_id=getattr(request.state, "request_id", ""),
    )
    return job_id


class _FdHolder:
    """Owns one received descriptor; close() is idempotent (must close exactly once)."""

    __slots__ = ("_closed", "fd")

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            close_fds([self.fd])


async def _read_fd_action(request: Request, path: str) -> tuple[dict[str, Any], _FdHolder]:
    """``files.read_fd`` via call_with_fds; the returned holder owns the descriptor."""
    helper = request.app.state.helper
    try:
        result, fds = await asyncio.to_thread(
            helper.call_with_fds, "files.read_fd", {"path": path}, timeout_s=_HELPER_TIMEOUT_S
        )
    except HelperActionError as exc:
        raise _semantic(exc) from exc
    if len(fds) != 1:
        close_fds(fds)
        raise HelperUnavailableError("helper returned an unexpected descriptor count")
    return result, _FdHolder(fds[0])


class _RangeNotSatisfiable(Exception):
    """Requested range lies entirely beyond EOF (HTTP 416)."""


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Single-range support; None serves the full body (malformed ranges ignored).

    Raises:
        _RangeNotSatisfiable: Syntactically valid range beyond EOF.
    """
    if header is None:
        return None
    match = _RANGE_RE.match(header.strip())
    if match is None:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:  # suffix form: last N bytes
        n = int(last)
        if n == 0 or size == 0:
            raise _RangeNotSatisfiable()
        return max(size - n, 0), size - 1
    start = int(first)
    if start >= size:
        raise _RangeNotSatisfiable()
    end = size - 1 if not last else min(int(last), size - 1)
    if end < start:
        return None
    return start, end


async def _stream_fd(holder: _FdHolder, start: int, length: int) -> AsyncIterator[bytes]:
    offset, remaining = start, length
    try:
        while remaining > 0:
            chunk = await asyncio.to_thread(
                os.pread, holder.fd, min(_CHUNK_BYTES, remaining), offset
            )
            if not chunk:
                break
            offset += len(chunk)
            remaining -= len(chunk)
            yield chunk
    finally:
        holder.close()


def _content_disposition(name: str, *, disposition: str = "attachment") -> str:
    fallback = "".join(ch if " " <= ch < "\x7f" and ch not in '"\\' else "_" for ch in name)
    safe = fallback or "download"
    quoted = urllib.parse.quote(name, safe="")
    return f"{disposition}; filename=\"{safe}\"; filename*=UTF-8''{quoted}"


# ---------------------------------------------------------------------------
# directory download: streamed zip
# ---------------------------------------------------------------------------


class _ZipBuffer(io.RawIOBase):
    """In-memory sink ``zipfile`` writes into, drained chunk-wise by the stream.

    Deliberately neither seekable nor tell-able: ZipFile then wraps it in its
    internal ``_Tellable`` and takes the unseekable write path (data
    descriptors instead of header seek-back), which is what a live HTTP
    stream requires.
    """

    def __init__(self) -> None:
        super().__init__()
        self._chunks: deque[bytes] = deque()

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        data = bytes(b)
        if data:
            self._chunks.append(data)
        return len(data)

    def drain(self) -> list[bytes]:
        out = list(self._chunks)
        self._chunks.clear()
        return out


def _zip_date_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    clamped = min(max(mtime, _ZIP_TIME_MIN), _ZIP_TIME_MAX)
    t = time.localtime(clamped)
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def _zip_dir_entry(zf: zipfile.ZipFile, rel: str, mtime: float, mode: int) -> None:
    info = zipfile.ZipInfo(rel.rstrip("/") + "/", date_time=_zip_date_time(mtime))
    # Unix type+permission bits in the high word, MS-DOS directory bit low.
    info.external_attr = ((stat.S_IFDIR | (mode & 0o7777)) << 16) | 0x10
    zf.writestr(info, b"")


def _pump_chunk(fd: int, offset: int, writer: IO[bytes]) -> int:
    """Read one chunk at ``offset`` and feed it to the zip member writer."""
    chunk = os.pread(fd, _CHUNK_BYTES, offset)
    if chunk:
        writer.write(chunk)
    return len(chunk)


async def _zip_member(
    request: Request,
    zf: zipfile.ZipFile,
    buf: _ZipBuffer,
    abs_path: str,
    arc_name: str,
    entry: dict[str, Any],
) -> AsyncIterator[bytes]:
    """Stream one regular file into the archive, yielding compressed chunks."""
    try:
        result, holder = await _read_fd_action(request, abs_path)
    except (NotFoundError, HelperActionError):
        return  # vanished or stopped being a regular file since listing: skip
    try:
        size = int(result["size"])
        info = zipfile.ZipInfo(arc_name, date_time=_zip_date_time(float(entry["mtime"])))
        info.external_attr = (stat.S_IFREG | (int(entry["mode"]) & 0o7777)) << 16
        info.file_size = size
        info.compress_type = zipfile.ZIP_DEFLATED
        with zf.open(info, mode="w", force_zip64=size >= _ZIP64_THRESHOLD) as writer:
            offset = 0
            while True:
                n = await asyncio.to_thread(_pump_chunk, holder.fd, offset, writer)
                if n == 0:
                    break
                offset += n
                for piece in buf.drain():
                    yield piece
    finally:
        holder.close()
    for piece in buf.drain():
        yield piece


async def _zip_walk(
    request: Request,
    zf: zipfile.ZipFile,
    buf: _ZipBuffer,
    root: str,
    arc_root: str,
    root_stat: dict[str, Any],
) -> AsyncIterator[bytes]:
    """Walk ``root`` via the helper, emitting its subtree into an open archive.

    Entries that disappear or turn unreadable mid-walk are skipped so the
    download always completes; symlinks and special files are never archived
    (same policy as ``files.archive``). Only ``HelperUnavailableError`` (dead
    helper) aborts the stream.
    """
    stack: list[tuple[str, str, int, float, int]] = [
        (
            root,
            arc_root,
            0,
            float(root_stat.get("mtime", 0.0)),
            int(root_stat.get("mode", 0o755)),
        )
    ]
    while stack:
        abs_dir, rel_dir, depth, mtime, mode = stack.pop()
        try:
            listing = await _files_call(
                request, "files.list", {"path": abs_dir, "include_hidden": True}
            )
        except (NotFoundError, HelperActionError):
            continue  # subtree vanished or is unreadable: skip it
        _zip_dir_entry(zf, rel_dir, mtime, mode)
        for piece in buf.drain():
            yield piece
        for entry in listing.get("entries", []):
            child_abs = os.path.join(abs_dir, str(entry["name"]))
            child_rel = f"{rel_dir}/{entry['name']}"
            if entry["type"] == "dir":
                if depth < _ZIP_MAX_DEPTH and child_abs not in _ZIP_PRUNE_ROOTS:
                    stack.append(
                        (child_abs, child_rel, depth + 1, float(entry["mtime"]), int(entry["mode"]))
                    )
            elif entry["type"] == "file":
                async for piece in _zip_member(request, zf, buf, child_abs, child_rel, entry):
                    yield piece
            # symlinks/sockets/fifos/devices cannot be represented safely: skip


def _unique_arc_name(name: str, taken: set[str]) -> str:
    """Disambiguate a top-level archive entry name (``a.txt`` → ``a (2).txt``)."""
    if name not in taken:
        taken.add(name)
        return name
    stem, ext = os.path.splitext(name)
    for n in itertools.count(2):
        candidate = f"{stem} ({n}){ext}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover


async def _zip_stream(
    request: Request, items: list[tuple[str, dict[str, Any]]]
) -> AsyncIterator[bytes]:
    """Stream ``items`` — (abs_path, stat) pairs — as one zip archive.

    Files and directories may be mixed; directories are walked recursively.
    Top-level names are de-duplicated so selecting same-named entries from
    different places still produces a well-formed archive.
    """
    buf = _ZipBuffer()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    taken: set[str] = set()
    for path, info in items:
        arc_name = _unique_arc_name(os.path.basename(path) or "root", taken)
        if str(info.get("type")) == "dir":
            async for piece in _zip_walk(request, zf, buf, path, arc_name, info):
                yield piece
        else:
            async for piece in _zip_member(request, zf, buf, path, arc_name, info):
                yield piece
    zf.close()
    for piece in buf.drain():
        yield piece


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _read_capped(fd: int, cap: int) -> bytes:
    buf = bytearray()
    while len(buf) <= cap:
        chunk = os.read(fd, _CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


async def _stage_and_install(
    request: Request,
    target: str,
    expected_etag: str | None,
    *,
    source: UploadFile | bytes,
    max_bytes: int,
) -> str:
    """Stream ``source`` into a helper staging file and atomically install it.

    Returns the new etag. The staging file is best-effort deleted on failure
    (``files.install`` also cleans up on its own failure paths).

    Raises:
        PayloadTooLargeError: The streamed source exceeded ``max_bytes``.
    """
    helper = request.app.state.helper
    parent = os.path.dirname(target) or "/"
    try:
        opened, fds = await asyncio.to_thread(
            helper.call_with_fds, "files.open_write", {"dir": parent}, timeout_s=_HELPER_TIMEOUT_S
        )
    except HelperActionError as exc:
        raise _semantic(exc) from exc
    if len(fds) != 1:
        close_fds(fds)
        raise HelperUnavailableError("helper returned an unexpected descriptor count")
    fd = fds[0]
    staged = str(opened["staged_path"])
    try:
        try:
            if isinstance(source, bytes):
                await asyncio.to_thread(_write_all, fd, source)
            else:
                total = 0
                while True:
                    chunk = await source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise PayloadTooLargeError(f"upload exceeds the {max_bytes} byte limit")
                    await asyncio.to_thread(_write_all, fd, chunk)
        finally:
            close_fds([fd])
        result = await _files_call(
            request,
            "files.install",
            {"staged_path": staged, "target": target, "expected_etag": expected_etag, "mode": None},
            timeout_s=_MUTATE_TIMEOUT_S,
        )
    except BaseException:
        with contextlib.suppress(AppError):
            await helper.acall(
                "files.delete", {"path": staged, "recursive": False}, timeout_s=_HELPER_TIMEOUT_S
            )
        raise
    return str(result["etag"])


# ---------------------------------------------------------------------------
# read endpoints
# ---------------------------------------------------------------------------


@files_router.get("/list")
async def list_dir(
    request: Request,
    _auth: ActiveSessionDep,
    path: Annotated[str, Query(min_length=1, max_length=_PATH_MAX)],
    show_hidden: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Directory listing (lstat semantics; helper validates the path)."""
    result = await _files_call(request, "files.list", {"path": path, "include_hidden": show_hidden})
    return {"path": path, "entries": result.get("entries", [])}


@files_router.get("/stat")
async def stat_path(
    request: Request,
    _auth: ActiveSessionDep,
    path: Annotated[str, Query(min_length=1, max_length=_PATH_MAX)],
) -> dict[str, Any]:
    """lstat one path; symlinks carry a ``target`` field and are never followed."""
    result = await _files_call(request, "files.stat", {"path": path})
    return {"path": path, **result}


@files_router.get("/download")
async def download_file(
    request: Request,
    path: Annotated[str, Query(min_length=1, max_length=_PATH_MAX)],
    inline: Annotated[bool, Query()] = False,
) -> Response:
    """Stream a file, or a directory packed as a zip archive on the fly.

    Files support a single HTTP Range and, with ``inline=1`` and a
    whitelisted image extension, browser-embeddable preview headers.
    Directory archives are generated streamingly (unknown length, no Range).
    Auth uses :func:`resolve_stream_auth` so no DB session is held open for
    the potentially long-lived response.
    """
    await resolve_stream_auth(request)
    target = await _files_call(request, "files.stat", {"path": path})
    kind = str(target.get("type"))
    if kind == "dir":
        zip_name = (os.path.basename(path) or "root") + ".zip"
        return StreamingResponse(
            _zip_stream(request, [(path, target)]),
            media_type="application/zip",
            headers={"Content-Disposition": _content_disposition(zip_name)},
        )
    if kind != "file":
        raise BadRequestError(f"cannot download a {kind}; only files and directories")
    result, holder = await _read_fd_action(request, path)
    size = int(result["size"])
    try:
        span = _parse_range(request.headers.get("range"), size)
    except _RangeNotSatisfiable:
        holder.close()
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    name = os.path.basename(path)
    mime = _INLINE_IMAGE_MIME.get(os.path.splitext(name)[1].lower()) if inline else None
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(
            name, disposition="inline" if mime else "attachment"
        ),
        "ETag": f'"{result["etag"]}"',
    }
    if span is None:
        start, end, status = 0, size - 1, 200
    else:
        (start, end), status = span, 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = max(end - start + 1, 0)
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        _stream_fd(holder, start, length),
        status_code=status,
        headers=headers,
        media_type=mime or "application/octet-stream",
        background=BackgroundTask(holder.close),
    )


@files_router.get("/download-zip")
async def download_zip(
    request: Request,
    path: Annotated[str, Query(min_length=1, max_length=_PATH_MAX)],
    name: Annotated[list[str], Query(min_length=1, max_length=_DOWNLOAD_ZIP_MAX_ITEMS)],
) -> Response:
    """Stream a zip of several entries selected inside the directory ``path``.

    Taking a parent directory plus bare ``name`` values (rather than N full
    paths) keeps the URL short and lets every name be rejected outright if it
    contains a separator or ``..`` — traversal never reaches the helper. The
    archive is generated on the fly, so nothing is staged on disk (unlike
    ``/files/archive``, which writes a real archive file and is the right tool
    for huge selections).

    Missing entries are skipped rather than failing the whole download; a
    selection that resolves to nothing is a 404.
    """
    await resolve_stream_auth(request)
    names = list(dict.fromkeys(name))  # de-duplicate, keep the client's order
    for entry in names:
        if not entry or entry in (".", "..") or "/" in entry or "\0" in entry:
            raise BadRequestError(f"invalid entry name: {entry!r}")

    items: list[tuple[str, dict[str, Any]]] = []
    for entry in names:
        abs_path = os.path.join(path, entry)
        try:
            info = await _files_call(request, "files.stat", {"path": abs_path})
        except (NotFoundError, HelperActionError):
            continue  # vanished between listing and download: skip it
        if str(info.get("type")) in ("file", "dir"):
            items.append((abs_path, info))
    if not items:
        raise NotFoundError("没有可下载的条目")

    zip_name = (os.path.basename(path.rstrip("/")) or "root") + ".zip"
    return StreamingResponse(
        _zip_stream(request, items),
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(zip_name)},
    )


@files_router.get("/content")
async def read_content(
    request: Request,
    _auth: ActiveSessionDep,
    path: Annotated[str, Query(min_length=1, max_length=_PATH_MAX)],
) -> dict[str, Any]:
    """Text preview/editor read; files beyond the size cap are refused with 413."""
    result, holder = await _read_fd_action(request, path)
    try:
        if int(result["size"]) > _CONTENT_MAX_BYTES:
            raise PayloadTooLargeError(
                f"file larger than the {_CONTENT_MAX_BYTES} byte preview limit"
            )
        data = await asyncio.to_thread(_read_capped, holder.fd, _CONTENT_MAX_BYTES)
    finally:
        holder.close()
    truncated = len(data) > _CONTENT_MAX_BYTES
    if truncated:
        data = data[:_CONTENT_MAX_BYTES]
    return {
        "content": data.decode("utf-8", "replace"),
        "etag": str(result["etag"]),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# mutating endpoints
# ---------------------------------------------------------------------------


@files_router.put("/content", dependencies=[Depends(require_csrf)])
async def write_content(
    body: WriteContentRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Save editor content atomically; a stale ``expected_etag`` answers 409."""
    data = body.content.encode("utf-8")
    try:
        if len(data) > _CONTENT_MAX_BYTES:
            raise PayloadTooLargeError(
                f"content exceeds the {_CONTENT_MAX_BYTES} byte editor limit"
            )
        etag = await _stage_and_install(
            request, body.path, body.expected_etag, source=data, max_bytes=_CONTENT_MAX_BYTES
        )
    except AppError as exc:
        await _audit(db, request, auth, "files.write", body.path, error=exc)
        raise
    await _audit(db, request, auth, "files.write", body.path)
    return {"etag": etag}


@files_router.post("/upload", dependencies=[Depends(require_csrf)])
async def upload_file(
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Multipart upload streamed into the staging flow; over-limit answers 413.

    Fields: ``path`` (absolute destination file path), ``file`` (the payload),
    optional ``expected_etag``.
    """
    max_bytes = settings.limits.upload_max_bytes
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > max_bytes + _UPLOAD_CL_SLACK:
        raise PayloadTooLargeError(f"upload exceeds the {max_bytes} byte limit")
    # Bound the raw request body ourselves: request.form() would otherwise spool
    # the whole multipart file part to disk before any size check, and the
    # Content-Length pre-check is bypassable with Transfer-Encoding: chunked.
    _limit_request_body(request, max_bytes + _UPLOAD_CL_SLACK)
    try:
        form = await request.form()
    except _BodyTooLargeError as exc:
        raise PayloadTooLargeError(f"upload exceeds the {max_bytes} byte limit") from exc
    target = form.get("path")
    upload = form.get("file")
    expected = form.get("expected_etag")
    target_str = target if isinstance(target, str) else ""
    try:
        try:
            if not target_str:
                raise BadRequestError("multipart field 'path' must be the destination file path")
            if not isinstance(upload, UploadFile):
                raise BadRequestError("multipart field 'file' is required")
            expected_etag = expected if isinstance(expected, str) and expected else None
            etag = await _stage_and_install(
                request, target_str, expected_etag, source=upload, max_bytes=max_bytes
            )
        except AppError as exc:
            await _audit(db, request, auth, "files.upload", target_str, error=exc)
            raise
    finally:
        if isinstance(upload, UploadFile):
            await upload.close()
    await _audit(db, request, auth, "files.upload", target_str)
    return {"path": target_str, "etag": etag}


@files_router.post("/mkdir", dependencies=[Depends(require_csrf)])
async def make_dir(
    body: MkdirRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Create a directory; an existing path answers 409."""
    return await _audited_call(
        request,
        db,
        auth,
        action="files.mkdir",
        helper_action="files.mkdir",
        args={"path": body.path, "mode": body.mode, "parents": body.parents},
        target=body.path,
    )


@files_router.post("/rename", dependencies=[Depends(require_csrf)])
async def rename_path(
    body: RenameRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Rename/move within one filesystem; an existing destination answers 409."""
    return await _audited_call(
        request,
        db,
        auth,
        action="files.rename",
        helper_action="files.rename",
        args={"src": body.src, "dst": body.dst},
        target=f"{body.src} -> {body.dst}",
    )


@files_router.post("/copy", dependencies=[Depends(require_csrf)])
async def copy_path(
    body: CopyRequest,
    request: Request,
    response: Response,
    auth: ActiveSessionDep,
    db: DbDep,
) -> dict[str, Any]:
    """Copy a file synchronously; recursive tree copies run as a job (202)."""
    target = f"{body.src} -> {body.dst}"
    args: dict[str, Any] = {"src": body.src, "dst": body.dst, "recursive": body.recursive}
    if not body.recursive:
        return await _audited_call(
            request,
            db,
            auth,
            action="files.copy",
            helper_action="files.copy",
            args=args,
            target=target,
        )
    job_id = await _submit_helper_job(
        request,
        type_="files.copy",
        title=f"Copy {body.src} to {body.dst}",
        helper_action="files.copy",
        args=args,
        resource_key=f"files:{body.dst}",
    )
    await _audit(db, request, auth, "files.copy", target)
    response.status_code = 202
    return {"job_id": job_id}


@files_router.post("/chmod", dependencies=[Depends(require_csrf)])
async def chmod_path(
    body: ChmodRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Change permission bits; symlink targets are refused by the helper."""
    return await _audited_call(
        request,
        db,
        auth,
        action="files.chmod",
        helper_action="files.chmod",
        args={"path": body.path, "mode": body.mode},
        target=f"{body.path}:{body.mode:o}",
    )


@files_router.post("/chown", dependencies=[Depends(require_csrf)])
async def chown_path(
    body: ChownRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Change owner/group (``-1`` keeps the current value); never follows symlinks."""
    return await _audited_call(
        request,
        db,
        auth,
        action="files.chown",
        helper_action="files.chown",
        args={"path": body.path, "uid": body.uid, "gid": body.gid},
        target=f"{body.path}:{body.uid}:{body.gid}",
    )


@files_router.delete("", dependencies=[Depends(require_csrf), Depends(require_recent_reauth)])
async def delete_path(
    body: DeleteRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Delete a file/directory (recent re-auth required); symlinks are unlinked, not followed."""
    return await _audited_call(
        request,
        db,
        auth,
        action="files.delete",
        helper_action="files.delete",
        args={"path": body.path, "recursive": body.recursive},
        target=body.path,
    )


@files_router.post("/archive", status_code=202, dependencies=[Depends(require_csrf)])
async def archive_paths(
    body: ArchiveRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Pack paths into an archive as a background job; answers ``{"job_id"}``."""
    job_id = await _submit_helper_job(
        request,
        type_="files.archive",
        title=f"Archive to {body.dst}",
        helper_action="files.archive",
        args={"paths": body.paths, "dst": body.dst, "format": body.format},
        resource_key=f"files:{body.dst}",
    )
    await _audit(db, request, auth, "files.archive", body.dst)
    return {"job_id": job_id}


@files_router.post("/extract", status_code=202, dependencies=[Depends(require_csrf)])
async def extract_archive(
    body: ExtractRequest, request: Request, auth: ActiveSessionDep, db: DbDep
) -> dict[str, Any]:
    """Extract an archive as a background job (Zip-Slip/bomb guarded in the helper)."""
    job_id = await _submit_helper_job(
        request,
        type_="files.extract",
        title=f"Extract {body.archive}",
        helper_action="files.extract",
        args={"archive": body.archive, "dst_dir": body.dst_dir, "format": body.format},
        resource_key=f"files:{body.dst_dir}",
    )
    await _audit(db, request, auth, "files.extract", f"{body.archive} -> {body.dst_dir}")
    return {"job_id": job_id}
