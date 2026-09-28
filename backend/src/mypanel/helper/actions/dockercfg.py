"""Helper actions for the Docker daemon configuration file (``daemon.json``).

``/etc/docker/daemon.json`` is root-owned, so reads and writes go through the
root helper. Unlike the generic ``files.*`` actions, the target path here is a
compile-time constant rather than an argument: these actions can only ever
touch the daemon config, which keeps the audit trail unambiguous and removes
path handling from the attack surface entirely.

Every write is validated as a JSON **object** first (a daemon that fails to
parse its config refuses to start, and a broken restart is far more painful
than a rejected save), then the previous content is copied to a timestamped
``daemon.json.bak-*`` sibling before the atomic replace. Backups are pruned to
the most recent :data:`_KEEP_BACKUPS`.

Restarting the daemon is *not* done here — the API reuses the existing
``systemd.restart`` action with ``docker.service``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from mypanel.helper.actions.files import _map_os_errors, _open_nofollow
from mypanel.helper.registry import ActionError, register
from mypanel.ipc.messages import HELPER_INVALID_ARGS, HELPER_UNSUPPORTED

#: The one file these actions may touch. Not user input, by design.
DAEMON_JSON = "/etc/docker/daemon.json"
#: Directory holding it; created on demand when Docker was installed without one.
DAEMON_DIR = "/etc/docker"

_MAX_CONFIG_BYTES = 256 * 1024
_STAGED_PREFIX = ".mypanel-dockercfg-"
_BACKUP_PREFIX = "daemon.json.bak-"
_BACKUP_RE = re.compile(r"^daemon\.json\.bak-")
_KEEP_BACKUPS = 5


class EmptyArgs(BaseModel):
    pass


class WriteArgs(BaseModel):
    content: str = Field(max_length=_MAX_CONFIG_BYTES)


def _write_atomic(target: str, data: bytes, mode: int) -> None:
    """Write ``data`` to ``target`` via a staged temp file + fsync + atomic rename.

    Mirrors ``nginx._write_atomic``: the staging file is created
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` in the target's own directory (same
    filesystem, so the rename is atomic), fsync'd, then renamed over the target;
    the directory is fsync'd so the rename is durable. Every failure path
    removes the staging file.
    """
    directory = os.path.dirname(target)
    staged = os.path.join(directory, _STAGED_PREFIX + secrets.token_hex(8))
    with _map_os_errors(directory):
        fd = _open_nofollow(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with suppress(OSError):
            os.unlink(staged)
        raise
    os.close(fd)
    try:
        os.chmod(staged, mode)  # O_CREAT mode is masked by umask; force it exactly
        os.rename(staged, target)
    except BaseException:
        with suppress(OSError):
            os.unlink(staged)
        raise
    with suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _read_bytes(path: str, cap: int) -> bytes:
    """Bounded, symlink-refusing read of a regular file."""
    with _map_os_errors(path):
        fd = _open_nofollow(path, os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ActionError(HELPER_UNSUPPORTED, f"not a regular file: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)[:cap]


def _parse_object(content: str) -> dict[str, Any]:
    """Validate ``content`` as a JSON object, the only shape dockerd accepts."""
    stripped = content.strip()
    if not stripped:
        # An empty file is not valid JSON; treat "clear the config" as "{}".
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ActionError(
            HELPER_INVALID_ARGS, f"daemon.json 不是合法 JSON (第 {exc.lineno} 行 {exc.msg})"
        ) from exc
    if not isinstance(parsed, dict):
        raise ActionError(HELPER_INVALID_ARGS, "daemon.json 顶层必须是一个 JSON 对象")
    return parsed


def _prune_backups() -> None:
    """Keep only the newest :data:`_KEEP_BACKUPS` ``daemon.json.bak-*`` files."""
    with suppress(OSError):
        names = sorted(
            (n for n in os.listdir(DAEMON_DIR) if _BACKUP_RE.match(n)),
            reverse=True,  # the timestamp suffix sorts lexicographically
        )
        for stale in names[_KEEP_BACKUPS:]:
            with suppress(OSError):
                os.unlink(os.path.join(DAEMON_DIR, stale))


@register("dockercfg.read", EmptyArgs)
def dockercfg_read(args: EmptyArgs) -> dict[str, Any]:
    """Read daemon.json; a missing file is reported, not an error.

    Docker runs perfectly well with no daemon.json at all, so ``exists=false``
    with empty content is a normal state the UI renders as "尚未配置".
    """
    try:
        st = os.stat(DAEMON_JSON)
    except FileNotFoundError:
        return {"path": DAEMON_JSON, "exists": False, "content": "", "mtime": 0.0}
    except OSError as exc:
        raise ActionError(HELPER_UNSUPPORTED, f"cannot stat {DAEMON_JSON}: {exc}") from exc
    raw = _read_bytes(DAEMON_JSON, _MAX_CONFIG_BYTES)
    return {
        "path": DAEMON_JSON,
        "exists": True,
        "content": raw.decode("utf-8", "replace"),
        "mtime": st.st_mtime,
    }


@register("dockercfg.write", WriteArgs)
def dockercfg_write(args: WriteArgs) -> dict[str, Any]:
    """Validate, back up and atomically replace daemon.json.

    Returns the normalised content actually written plus the backup filename
    (empty when there was no previous file).
    """
    parsed = _parse_object(args.content)
    # Re-serialise so the stored file is always canonical, readable JSON —
    # whatever whitespace the client sent, the daemon sees the same shape.
    normalised = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    with _map_os_errors(DAEMON_DIR):
        os.makedirs(DAEMON_DIR, mode=0o755, exist_ok=True)

    backup = ""
    # Back up whatever is there now. A read failure on an existing file is a
    # hard error: silently skipping the backup and then overwriting would be
    # the one case where the user loses a config they cannot recover.
    if os.path.lexists(DAEMON_JSON):
        previous = _read_bytes(DAEMON_JSON, _MAX_CONFIG_BYTES)
        if previous:
            backup = _BACKUP_PREFIX + datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
            _write_atomic(os.path.join(DAEMON_DIR, backup), previous, 0o644)
            _prune_backups()

    _write_atomic(DAEMON_JSON, normalised.encode("utf-8"), 0o644)
    return {"path": DAEMON_JSON, "content": normalised, "backup": backup}
