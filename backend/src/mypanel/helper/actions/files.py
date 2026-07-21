"""M1 file-inspection actions: ping, files.stat and files.list.

All paths must be absolute and already normalised; symlinks are shown with
lstat and never followed.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel

import mypanel
from mypanel.helper.registry import ActionError, register
from mypanel.ipc.messages import (
    HELPER_DENIED,
    HELPER_NOT_A_DIRECTORY,
    HELPER_PATH_INVALID,
    HELPER_PATH_NOT_FOUND,
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
