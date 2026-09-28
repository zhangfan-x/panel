"""File actions: inspection (ping/stat/list) and M3 mutation/staging/archives.

All paths must be absolute and already normalised; symlinks are shown with
lstat and never followed. Write/delete targets are additionally guarded by
:func:`_ensure_mutable` (no mutations whose real target lives under ``/proc``,
``/sys`` or ``/dev``) and final-component symlinks are never written through.
"""

from __future__ import annotations

import errno
import gzip
import os
import secrets
import shutil
import stat
import tarfile
import threading
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import IO, Any, Literal

from pydantic import BaseModel, Field

import mypanel
from mypanel.helper.registry import ActionError, ActionOutcome, register
from mypanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_DENIED,
    HELPER_NOT_A_DIRECTORY,
    HELPER_PATH_INVALID,
    HELPER_PATH_NOT_FOUND,
    HELPER_TOO_LARGE,
    HELPER_UNSUPPORTED,
)


class PingArgs(BaseModel):
    pass


class StatArgs(BaseModel):
    path: str


class ListArgs(BaseModel):
    path: str
    include_hidden: bool = False


def _validate_path(path: str) -> str:
    """Require an absolute, NUL-free path that normpath leaves unchanged.

    Rejects `..` segments, duplicate separators and trailing slashes ("/" itself
    is fine). A leading "//" survives normpath on POSIX, so it is rejected
    explicitly. Returns the path unchanged.
    """
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\0" in path
        or os.path.normpath(path) != path
    ):
        raise ActionError(HELPER_PATH_INVALID, f"invalid path: {path!r}")
    return path


def _json_safe_fs_str(name: str) -> str:
    """Make a filesystem string safe to JSON-encode as UTF-8.

    Linux filenames and symlink targets are arbitrary byte strings; the os layer
    decodes non-UTF-8 bytes into ``str`` with lone surrogates (surrogateescape).
    ``json.dumps(...).encode("utf-8")`` on the wire would then raise
    UnicodeEncodeError. Round-trip through surrogateescape to recover the raw
    bytes and re-decode with ``replace`` so undecodable bytes become U+FFFD,
    yielding a displayable, losslessly-JSON-encodable string.
    """
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


@contextmanager
def _map_os_errors(path: str) -> Iterator[None]:
    """Translate filesystem OSErrors into stable helper error codes."""
    try:
        yield
    except FileNotFoundError as exc:
        raise ActionError(HELPER_PATH_NOT_FOUND, f"path not found: {path}") from exc
    except NotADirectoryError as exc:
        raise ActionError(HELPER_NOT_A_DIRECTORY, f"not a directory: {path}") from exc
    except PermissionError as exc:
        raise ActionError(HELPER_DENIED, f"permission denied: {path}") from exc


def _file_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISBLK(mode):
        return "block"
    if stat.S_ISCHR(mode):
        return "char"
    return "unknown"


def _stat_fields(st: os.stat_result) -> dict[str, Any]:
    return {
        "type": _file_type(st.st_mode),
        "size": st.st_size,
        "mode": stat.S_IMODE(st.st_mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mtime": st.st_mtime,
    }


@register("ping", PingArgs)
def ping(args: PingArgs) -> dict[str, Any]:
    return {"pong": True, "helper_version": mypanel.__version__}


@register("files.stat", StatArgs)
def files_stat(args: StatArgs) -> dict[str, Any]:
    path = _validate_path(args.path)
    with _map_os_errors(path):
        st = os.lstat(path)
        payload = _stat_fields(st)
        if stat.S_ISLNK(st.st_mode):
            payload["target"] = _json_safe_fs_str(os.readlink(path))
    return payload


@register("files.list", ListArgs)
def files_list(args: ListArgs) -> dict[str, Any]:
    path = _validate_path(args.path)
    entries: list[dict[str, Any]] = []
    with _map_os_errors(path), os.scandir(path) as scan:
        for entry in scan:
            if not args.include_hidden and entry.name.startswith("."):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # entry vanished between scandir and stat
            item = _stat_fields(st)
            item["name"] = _json_safe_fs_str(entry.name)
            entries.append(item)
    entries.sort(key=lambda item: item["name"])
    return {"entries": entries}


# ---------------------------------------------------------------------------
# M3 mutation actions (docs/code-plan-m2-m5.md §3.3)
# ---------------------------------------------------------------------------

# Real targets under these roots must never be written, deleted or re-attributed.
_READONLY_ROOTS: tuple[str, ...] = ("/proc", "/sys", "/dev")
_STAGED_PREFIX = ".mypanel-tmp-"
_COPY_BUF_BYTES = 1024 * 1024
# Decompression-bomb caps for files.extract (module-level so tests can patch them).
_EXTRACT_MAX_ENTRIES = 20_000
_EXTRACT_MAX_BYTES = 4 * 1024 * 1024 * 1024

# Serialises the re-stat + rename critical section of files.install so two
# concurrent saves with expected_etag cannot both pass the etag check.
_install_lock = threading.Lock()


def _etag(st: os.stat_result) -> str:
    """Cheap change detector for optimistic concurrency: ``"{size}-{mtime_ns}"``."""
    return f"{st.st_size}-{st.st_mtime_ns}"


def _ensure_mutable(path: str) -> None:
    """Shared write/delete guard: deny mutations under /proc, /sys and /dev.

    The parent directory chain is resolved with ``realpath`` (so a symlinked
    directory cannot smuggle a mutation into a kernel filesystem) but the final
    component is left unresolved: unlinking or replacing a symlink that merely
    points into ``/proc`` is safe and allowed.
    """
    if path == "/":
        raise ActionError(HELPER_DENIED, "refusing to modify the filesystem root")
    parent = os.path.realpath(os.path.dirname(path))
    real = os.path.join(parent, os.path.basename(path))
    for root in _READONLY_ROOTS:
        if real == root or real.startswith(root + "/"):
            raise ActionError(HELPER_DENIED, f"read-only system path: {path}")


def _open_nofollow(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
    """``os.open`` with O_NOFOLLOW|O_CLOEXEC; a symlink final component is denied."""
    try:
        return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ActionError(HELPER_DENIED, f"refusing to follow symlink: {path}") from exc
        raise


def _lstat_or_none(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


class ReadFdArgs(BaseModel):
    path: str


@register("files.read_fd", ReadFdArgs)
def files_read_fd(args: ReadFdArgs) -> ActionOutcome:
    """Open a regular file read-only and pass the descriptor to the API.

    O_NONBLOCK prevents hanging on FIFOs; it is irrelevant for regular files.
    Non-regular files (and symlink final components, via O_NOFOLLOW) are refused.
    """
    path = _validate_path(args.path)
    with _map_os_errors(path):
        fd = _open_nofollow(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ActionError(HELPER_UNSUPPORTED, f"not a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return ActionOutcome(
        result={"size": st.st_size, "mtime": st.st_mtime, "etag": _etag(st)}, fds=[fd]
    )


class OpenWriteArgs(BaseModel):
    dir: str
    mode: int = Field(default=0o644, ge=0, le=0o777)


@register("files.open_write", OpenWriteArgs)
def files_open_write(args: OpenWriteArgs) -> ActionOutcome:
    """Create an exclusive staging file inside ``dir`` and pass its write fd.

    The caller streams content into the descriptor, closes it, then finalises
    with ``files.install``. Orphaned ``.mypanel-tmp-*`` files (caller crashed
    between the two calls) are inert and may be deleted at any time.
    """
    dir_path = _validate_path(args.dir)
    name = _STAGED_PREFIX + secrets.token_hex(8)
    _ensure_mutable(os.path.join(dir_path, name))
    with _map_os_errors(dir_path):
        dir_fd = os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with _map_os_errors(dir_path):
            fd = _open_nofollow(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, args.mode, dir_fd=dir_fd
            )
    finally:
        os.close(dir_fd)
    return ActionOutcome(result={"staged_path": os.path.join(dir_path, name)}, fds=[fd])


class InstallArgs(BaseModel):
    staged_path: str
    target: str
    expected_etag: str | None = None
    mode: int | None = Field(default=None, ge=0, le=0o7777)


@register("files.install", InstallArgs)
def files_install(args: InstallArgs) -> dict[str, Any]:
    """Atomically move a staged file onto its target (fsync → etag check → rename).

    Only files created by ``files.open_write`` in the target's own directory are
    accepted (same-filesystem rename, no cross-directory laundering). When the
    target exists its mode/uid/gid are preserved onto the replacement unless an
    explicit ``mode`` is given; a symlink target is never written through. With
    ``expected_etag`` the target is re-statted under a lock and a mismatch is a
    ``helper.conflict``. The staging file is removed on every failure path.
    """
    staged = _validate_path(args.staged_path)
    target = _validate_path(args.target)
    _ensure_mutable(target)
    if not os.path.basename(staged).startswith(_STAGED_PREFIX):
        raise ActionError(HELPER_DENIED, "staged_path was not created by files.open_write")
    if os.path.dirname(staged) != os.path.dirname(target):
        raise ActionError(HELPER_DENIED, "staged file must live in the target's directory")
    try:
        with _map_os_errors(staged):
            fd = _open_nofollow(staged, os.O_RDONLY)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ActionError(
                    HELPER_UNSUPPORTED, f"staged path is not a regular file: {staged}"
                )
            with _install_lock, _map_os_errors(target):
                existing = _lstat_or_none(target)
                if existing is not None and stat.S_ISLNK(existing.st_mode):
                    raise ActionError(HELPER_DENIED, f"refusing to replace a symlink: {target}")
                if existing is not None and not stat.S_ISREG(existing.st_mode):
                    raise ActionError(
                        HELPER_CONFLICT, f"target exists and is not a regular file: {target}"
                    )
                if args.expected_etag is not None and (
                    existing is None or _etag(existing) != args.expected_etag
                ):
                    raise ActionError(HELPER_CONFLICT, f"target changed on disk: {target}")
                if existing is not None:
                    # fchown first: chown by the owner clears setuid/setgid bits.
                    os.fchown(fd, existing.st_uid, existing.st_gid)
                if args.mode is not None:
                    os.fchmod(fd, args.mode)
                elif existing is not None:
                    os.fchmod(fd, stat.S_IMODE(existing.st_mode))
                os.fsync(fd)
                os.rename(staged, target)
        finally:
            os.close(fd)
        with _map_os_errors(target):
            dir_fd = os.open(
                os.path.dirname(target) or "/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return {"etag": _etag(os.lstat(target))}
    except BaseException:
        with suppress(OSError):
            os.unlink(staged)
        raise


class MkdirArgs(BaseModel):
    path: str
    mode: int = Field(default=0o755, ge=0, le=0o777)
    parents: bool = False


@register("files.mkdir", MkdirArgs)
def files_mkdir(args: MkdirArgs) -> dict[str, Any]:
    path = _validate_path(args.path)
    _ensure_mutable(path)
    try:
        with _map_os_errors(path):
            if args.parents:
                os.makedirs(path, mode=args.mode)
            else:
                os.mkdir(path, mode=args.mode)
    except FileExistsError as exc:
        raise ActionError(HELPER_CONFLICT, f"path already exists: {path}") from exc
    return {"ok": True}


class RenameArgs(BaseModel):
    src: str
    dst: str


@register("files.rename", RenameArgs)
def files_rename(args: RenameArgs) -> dict[str, Any]:
    src = _validate_path(args.src)
    dst = _validate_path(args.dst)
    _ensure_mutable(src)
    _ensure_mutable(dst)
    if _lstat_or_none(dst) is not None:
        raise ActionError(HELPER_CONFLICT, f"destination already exists: {dst}")
    try:
        with _map_os_errors(src):
            os.rename(src, dst)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise ActionError(
                HELPER_UNSUPPORTED, f"cross-device rename not supported (copy instead): {src}"
            ) from exc
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR):
            raise ActionError(HELPER_CONFLICT, f"destination already exists: {dst}") from exc
        raise
    return {"ok": True}


class CopyArgs(BaseModel):
    src: str
    dst: str
    recursive: bool = False


@register("files.copy", CopyArgs)
def files_copy(args: CopyArgs) -> dict[str, Any]:
    """Copy a file (or a tree with ``recursive``); symlinks are copied as links."""
    src = _validate_path(args.src)
    dst = _validate_path(args.dst)
    _ensure_mutable(dst)
    with _map_os_errors(src):
        st = os.lstat(src)
    if _lstat_or_none(dst) is not None:
        raise ActionError(HELPER_CONFLICT, f"destination already exists: {dst}")
    with _map_os_errors(dst):
        if stat.S_ISDIR(st.st_mode):
            if not args.recursive:
                raise ActionError(
                    HELPER_UNSUPPORTED, f"source is a directory (recursive required): {src}"
                )
            shutil.copytree(src, dst, symlinks=True)
        elif stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            shutil.copy2(src, dst, follow_symlinks=False)
        else:
            raise ActionError(HELPER_UNSUPPORTED, f"cannot copy special file: {src}")
    return {"ok": True}


class DeleteArgs(BaseModel):
    path: str
    recursive: bool = False


@register("files.delete", DeleteArgs)
def files_delete(args: DeleteArgs) -> dict[str, Any]:
    """Delete a file, symlink or directory; a symlink itself is removed, never followed."""
    path = _validate_path(args.path)
    _ensure_mutable(path)
    with _map_os_errors(path):
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode):
            if args.recursive:
                shutil.rmtree(path)  # refuses a symlink path, never follows links inside
            else:
                try:
                    os.rmdir(path)
                except OSError as exc:
                    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise ActionError(HELPER_CONFLICT, f"directory not empty: {path}") from exc
                    raise
        else:
            os.unlink(path)
    return {"ok": True}


class ChmodArgs(BaseModel):
    path: str
    mode: int = Field(ge=0, le=0o7777)


@register("files.chmod", ChmodArgs)
def files_chmod(args: ChmodArgs) -> dict[str, Any]:
    """chmod via an O_NOFOLLOW descriptor (race-free; symlink targets are denied)."""
    path = _validate_path(args.path)
    _ensure_mutable(path)
    with _map_os_errors(path):
        fd = _open_nofollow(path, os.O_RDONLY)
    try:
        with _map_os_errors(path):
            os.fchmod(fd, args.mode)
            return {"mode": stat.S_IMODE(os.fstat(fd).st_mode)}
    finally:
        os.close(fd)


class ChownArgs(BaseModel):
    path: str
    uid: int = Field(default=-1, ge=-1)
    gid: int = Field(default=-1, ge=-1)


@register("files.chown", ChownArgs)
def files_chown(args: ChownArgs) -> dict[str, Any]:
    """lchown (never follows symlinks); ``-1`` keeps the current uid/gid."""
    path = _validate_path(args.path)
    _ensure_mutable(path)
    with _map_os_errors(path):
        os.lchown(path, args.uid, args.gid)
        st = os.lstat(path)
    return {"uid": st.st_uid, "gid": st.st_gid}


class ArchiveArgs(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)
    dst: str
    format: Literal["zip", "tar", "tar.gz"]


@register("files.archive", ArchiveArgs)
def files_archive(args: ArchiveArgs) -> dict[str, Any]:
    """Pack files/directories into ``dst`` (created exclusively; removed on failure)."""
    sources = [_validate_path(p) for p in args.paths]
    dst = _validate_path(args.dst)
    _ensure_mutable(dst)
    for src in sources:
        with _map_os_errors(src):
            os.lstat(src)
    try:
        with _map_os_errors(dst):
            fd = _open_nofollow(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise ActionError(HELPER_CONFLICT, f"destination already exists: {dst}") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            if args.format == "zip":
                entries = _write_zip(fh, sources)
            else:
                entries = _write_tar(fh, sources, gz=args.format == "tar.gz")
    except BaseException:
        with suppress(OSError):
            os.unlink(dst)
        raise
    return {"ok": True, "entries": entries}


def _write_zip(fh: IO[bytes], sources: list[str]) -> int:
    count = 0
    with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in sources:
            base = _json_safe_fs_str(os.path.basename(src)) or "root"
            st = os.lstat(src)
            if stat.S_ISREG(st.st_mode):
                zf.write(src, base)
                count += 1
            elif stat.S_ISDIR(st.st_mode):
                for dirpath, _dirnames, filenames in os.walk(src, followlinks=False):
                    rel = os.path.relpath(dirpath, src)
                    arc_dir = base if rel == "." else os.path.join(base, _json_safe_fs_str(rel))
                    zf.writestr(arc_dir.rstrip("/") + "/", b"")
                    count += 1
                    for fname in filenames:
                        full = os.path.join(dirpath, fname)
                        if not stat.S_ISREG(os.lstat(full).st_mode):
                            continue  # zip cannot represent symlinks/special files safely
                        zf.write(full, os.path.join(arc_dir, _json_safe_fs_str(fname)))
                        count += 1
            else:
                raise ActionError(HELPER_UNSUPPORTED, f"cannot archive special file: {src}")
    return count


def _write_tar(fh: IO[bytes], sources: list[str], *, gz: bool) -> int:
    count = 0
    with tarfile.open(fileobj=fh, mode="w:gz" if gz else "w") as tf:
        for src in sources:
            st = os.lstat(src)
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise ActionError(HELPER_UNSUPPORTED, f"cannot archive special file: {src}")
            tf.add(src, arcname=os.path.basename(src) or "root", recursive=True)
            count += 1
    return count


class ExtractArgs(BaseModel):
    archive: str
    dst_dir: str
    format: Literal["zip", "tar", "tar.gz"]


@register("files.extract", ExtractArgs)
def files_extract(args: ExtractArgs) -> dict[str, Any]:
    """Extract an archive into ``dst_dir`` with Zip-Slip and bomb defences.

    Every member path is normalised and its (existing) parent chain resolved
    with ``realpath``; anything escaping ``dst_dir`` aborts with
    ``helper.denied``. Symlink/hardlink/device members are never materialised
    (counted as ``skipped``). Totals are capped by ``_EXTRACT_MAX_ENTRIES`` and
    ``_EXTRACT_MAX_BYTES`` (actual decompressed bytes, not declared sizes) —
    exceeding either aborts with ``helper.too_large``.
    """
    archive = _validate_path(args.archive)
    dst_dir = _validate_path(args.dst_dir)
    _ensure_mutable(os.path.join(dst_dir, "member"))
    with _map_os_errors(dst_dir):
        st = os.stat(dst_dir)
    if not stat.S_ISDIR(st.st_mode):
        raise ActionError(HELPER_NOT_A_DIRECTORY, f"not a directory: {dst_dir}")
    real_dst = os.path.realpath(dst_dir)
    with _map_os_errors(archive):
        afd = _open_nofollow(archive, os.O_RDONLY)
    with os.fdopen(afd, "rb") as fh:
        try:
            if args.format == "zip":
                extracted, skipped = _extract_zip(fh, real_dst)
            else:
                extracted, skipped = _extract_tar(fh, real_dst, gz=args.format == "tar.gz")
        except (
            zipfile.BadZipFile,
            tarfile.TarError,
            gzip.BadGzipFile,
            zlib.error,
            EOFError,
        ) as exc:
            raise ActionError(
                HELPER_UNSUPPORTED, f"corrupt or unsupported archive: {archive}"
            ) from exc
    return {"entries": extracted, "skipped": skipped}


def _safe_member_path(real_dst: str, name: str) -> str:
    """Absolute, contained output path for one archive member (Zip-Slip guard)."""
    if "\0" in name:
        raise ActionError(HELPER_DENIED, f"unsafe archive member name: {name!r}")
    norm = os.path.normpath(name)
    if os.path.isabs(norm) or norm == ".." or norm.startswith("../") or norm == ".":
        raise ActionError(HELPER_DENIED, f"unsafe archive member name: {name!r}")
    out = os.path.join(real_dst, norm)
    parent_real = os.path.realpath(os.path.dirname(out))
    if parent_real != real_dst and not parent_real.startswith(real_dst + os.sep):
        raise ActionError(HELPER_DENIED, f"archive member escapes destination: {name!r}")
    return out


def _write_member(src: IO[bytes], out: str, perm: int, total: int) -> int:
    """Stream one member to ``out`` (O_NOFOLLOW), enforcing the running byte cap."""
    fd = _open_nofollow(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, perm & 0o777)
    try:
        with src:
            while True:
                chunk = src.read(_COPY_BUF_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > _EXTRACT_MAX_BYTES:
                    raise ActionError(HELPER_TOO_LARGE, "extraction exceeds the total size limit")
                os.write(fd, chunk)
    except BaseException:
        os.close(fd)
        with suppress(OSError):
            os.unlink(out)
        raise
    os.close(fd)
    return total


def _extract_zip(fh: IO[bytes], real_dst: str) -> tuple[int, int]:
    extracted = skipped = total = 0
    with zipfile.ZipFile(fh) as zf:
        infos = zf.infolist()
        if len(infos) > _EXTRACT_MAX_ENTRIES:
            raise ActionError(HELPER_TOO_LARGE, "archive exceeds the entry count limit")
        if sum(info.file_size for info in infos) > _EXTRACT_MAX_BYTES:
            raise ActionError(HELPER_TOO_LARGE, "archive exceeds the total size limit")
        for info in infos:
            name = info.filename
            if name.rstrip("/") in ("", "."):
                continue
            if info.is_dir():
                os.makedirs(_safe_member_path(real_dst, name), exist_ok=True)
                extracted += 1
                continue
            mode_bits = (info.external_attr >> 16) & 0xFFFF
            # Zips written without unix type bits (S_IFMT == 0) are regular files;
            # explicit symlink/special type bits are never materialised.
            if stat.S_IFMT(mode_bits) not in (0, stat.S_IFREG):
                skipped += 1
                continue
            out = _safe_member_path(real_dst, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            perm = (stat.S_IMODE(mode_bits) & 0o777) or 0o644
            total = _write_member(zf.open(info), out, perm, total)
            extracted += 1
    return extracted, skipped


def _extract_tar(fh: IO[bytes], real_dst: str, *, gz: bool) -> tuple[int, int]:
    extracted = skipped = total = entries = 0
    with tarfile.open(fileobj=fh, mode="r:gz" if gz else "r:") as tf:
        for member in tf:
            entries += 1
            if entries > _EXTRACT_MAX_ENTRIES:
                raise ActionError(HELPER_TOO_LARGE, "archive exceeds the entry count limit")
            name = member.name
            if name.rstrip("/") in ("", "."):
                continue
            if member.isdir():
                os.makedirs(_safe_member_path(real_dst, name), exist_ok=True)
                extracted += 1
                continue
            if not member.isreg():
                skipped += 1  # symlink/hardlink/device member: never materialised
                continue
            out = _safe_member_path(real_dst, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            src = tf.extractfile(member)
            if src is None:  # pragma: no cover - isreg() members always extract
                skipped += 1
                continue
            total = _write_member(src, out, (member.mode & 0o777) or 0o644, total)
            extracted += 1
    return extracted, skipped
