"""Helper docker compose actions via subprocess (docs/code-plan-m2-m5.md §3.5).

Every invocation is ``/usr/bin/docker compose --project-directory <path> <verb>``
with an argument array and ``shell=False`` — project paths and names are never
interpolated into a shell string. The API validates that ``path`` lives under
``settings.docker.compose_root`` before calling; the helper re-checks only the
generic absolute/normalised path rules (it has no settings). Service lists are
parsed from the compose file itself with ``yaml.safe_load``. Long-running verbs
(``up``/``down``/``restart``) stream combined subprocess output through a pipe
descriptor and terminate it with an in-band :data:`EXIT_MARKER` line carrying
the exit code. Handlers self-register via ``@register`` at import time; this
module is imported by ``helper/actions/__init__.py`` so registration happens on
helper startup.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from contextlib import suppress
from typing import Any

import yaml
from pydantic import BaseModel

from ownpanel.helper.actions.files import (
    _json_safe_fs_str,
    _map_os_errors,
    _open_nofollow,
    _validate_path,
)
from ownpanel.helper.registry import ActionError, ActionOutcome, register
from ownpanel.ipc.messages import (
    HELPER_DOCKER_UNAVAILABLE,
    HELPER_INTERNAL,
    HELPER_NOT_A_DIRECTORY,
    HELPER_NOT_FOUND,
    HELPER_TIMEOUT,
    HELPER_TOO_LARGE,
    HELPER_UNSUPPORTED,
)

# Fixed binary path; tests monkeypatch this to a fake CLI.
_DOCKER = "/usr/bin/docker"

# Recognised compose file names, in lookup priority order (frontend contract).
COMPOSE_FILE_NAMES: tuple[str, ...] = ("compose.yaml", "compose.yml", "docker-compose.yml")

# Final line written into the up/down/restart output stream: marker + exit code.
EXIT_MARKER = "__OWNPANEL_COMPOSE_EXIT__ "

_MAX_COMPOSE_BYTES = 1024 * 1024
_CONFIG_TIMEOUT_S = 60.0
_PS_TIMEOUT_S = 30.0
_STDERR_TAIL_CHARS = 1000

_VERB_ARGS: dict[str, list[str]] = {
    # ``-d`` is mandatory: an attached ``up`` would never terminate the job.
    "up": ["up", "-d", "--remove-orphans"],
    "down": ["down"],
    "restart": ["restart"],
}


class DiscoverArgs(BaseModel):
    root: str


class ProjectArgs(BaseModel):
    path: str


def _tail(text: str) -> str:
    """Last chunk of subprocess stderr, JSON-safe and bounded."""
    cleaned = _json_safe_fs_str(text.strip())
    return cleaned[-_STDERR_TAIL_CHARS:]


def _find_compose_file(path: str) -> str | None:
    """First recognised compose file (regular file, lstat semantics) or None."""
    for name in COMPOSE_FILE_NAMES:
        try:
            st = os.lstat(os.path.join(path, name))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            return name
    return None


def _require_compose_file(path: str) -> str:
    """Compose file name of the project directory; raise when absent."""
    with _map_os_errors(path):
        st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise ActionError(HELPER_NOT_A_DIRECTORY, f"not a directory: {path}")
    name = _find_compose_file(path)
    if name is None:
        raise ActionError(HELPER_NOT_FOUND, f"no compose file in {path}")
    return name


def _run_compose(
    path: str, verb_args: list[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose`` to completion (argument array, shell=False)."""
    argv = [_DOCKER, "compose", "--project-directory", path, *verb_args]
    try:
        return subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ActionError(HELPER_DOCKER_UNAVAILABLE, "docker CLI not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ActionError(
            HELPER_TIMEOUT, f"docker compose {verb_args[0]} timed out after {timeout:.0f}s"
        ) from exc


def _read_compose_text(path: str, name: str) -> str:
    """Bounded, symlink-refusing read of the project's compose file."""
    full = os.path.join(path, name)
    with _map_os_errors(full):
        fd = _open_nofollow(full, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ActionError(HELPER_UNSUPPORTED, f"not a regular file: {full}")
        if st.st_size > _MAX_COMPOSE_BYTES:
            raise ActionError(HELPER_TOO_LARGE, f"compose file too large: {full}")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _services_from_doc(doc: Any) -> list[str] | None:
    """Sorted service names from a parsed compose document; None when malformed."""
    if not isinstance(doc, dict):
        return None
    services = doc.get("services")
    if not isinstance(services, dict) or not services:
        return None
    return sorted(str(key) for key in services)


@register("compose.discover", DiscoverArgs)
def compose_discover(args: DiscoverArgs) -> dict[str, Any]:
    """One-level scan of ``root`` for project directories and their compose files.

    A missing (or non-directory) root yields an empty list rather than an error:
    a fresh host simply has no compose projects yet.
    """
    root = _validate_path(args.root)
    projects: list[dict[str, Any]] = []
    try:
        scan = os.scandir(root)
    except (FileNotFoundError, NotADirectoryError):
        return {"projects": []}
    except PermissionError as exc:
        raise ActionError(HELPER_INTERNAL, f"cannot scan compose root: {root}") from exc
    with scan:
        for entry in scan:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            real_path = os.path.join(root, entry.name)
            compose_file = _find_compose_file(real_path)
            projects.append(
                {
                    "name": _json_safe_fs_str(entry.name),
                    "path": _json_safe_fs_str(real_path),
                    "has_compose_file": compose_file is not None,
                    "compose_file": compose_file,
                }
            )
    projects.sort(key=lambda item: str(item["name"]))
    return {"projects": projects}


@register("compose.config", ProjectArgs)
def compose_config(args: ProjectArgs) -> dict[str, Any]:
    """Validate a project's compose file; return the service list.

    The file must parse as YAML (``safe_load``) into a mapping with a non-empty
    ``services`` mapping. When the docker CLI is present the result is
    additionally checked with ``docker compose config --quiet``
    (``checked_by: "docker"``); without the CLI the YAML verdict stands alone
    (``checked_by: "yaml"``).
    """
    path = _validate_path(args.path)
    compose_file = _require_compose_file(path)
    text = _read_compose_text(path, compose_file)
    base: dict[str, Any] = {"compose_file": compose_file, "checked_by": "yaml"}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {**base, "valid": False, "services": [], "error": f"invalid YAML: {exc}"}
    services = _services_from_doc(doc)
    if services is None:
        return {
            **base,
            "valid": False,
            "services": [],
            "error": "compose file must be a mapping with a non-empty 'services' mapping",
        }
    try:
        result = _run_compose(path, ["config", "--quiet"], timeout=_CONFIG_TIMEOUT_S)
    except ActionError as exc:
        if exc.code == HELPER_DOCKER_UNAVAILABLE:
            return {**base, "valid": True, "services": services}
        raise
    if result.returncode != 0:
        return {
            **base,
            "checked_by": "docker",
            "valid": False,
            "services": services,
            "error": _tail(result.stderr) or f"docker compose config failed ({result.returncode})",
        }
    return {**base, "checked_by": "docker", "valid": True, "services": services}


def _parse_ps_output(output: str) -> list[dict[str, Any]]:
    """Parse ``docker compose ps --format json``: a JSON array or NDJSON lines."""
    text = output.strip()
    if not text:
        return []
    items: list[Any]
    try:
        loaded = json.loads(text)
        items = loaded if isinstance(loaded, list) else [loaded]
    except json.JSONDecodeError:
        items = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            with suppress(json.JSONDecodeError):
                items.append(json.loads(stripped))
    services: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        services.append(
            {
                "name": str(item.get("Name") or item.get("Service") or ""),
                "state": str(item.get("State") or ""),
                "status": str(item.get("Status") or ""),
                "ports": str(item.get("Ports") or ""),
            }
        )
    return services


@register("compose.ps", ProjectArgs)
def compose_ps(args: ProjectArgs) -> dict[str, Any]:
    """Container state of a project (``docker compose ps --all --format json``)."""
    path = _validate_path(args.path)
    _require_compose_file(path)
    result = _run_compose(path, ["ps", "--all", "--format", "json"], timeout=_PS_TIMEOUT_S)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "cannot connect" in stderr.lower() or "daemon" in stderr.lower():
            raise ActionError(
                HELPER_DOCKER_UNAVAILABLE, _tail(stderr) or "docker daemon unavailable"
            )
        raise ActionError(
            HELPER_INTERNAL, _tail(stderr) or f"docker compose ps failed ({result.returncode})"
        )
    return {"services": _parse_ps_output(result.stdout)}


def _compose_stream(path: str, verb: str) -> ActionOutcome:
    """Spawn a compose verb and hand back a pipe streaming its combined output.

    The child writes stdout+stderr into the pipe; a reaper thread waits for it,
    appends ``EXIT_MARKER <code>`` and closes the write end, so the reader sees
    output → marker → EOF. If the reader goes away early the child dies on
    SIGPIPE once the pipe buffer fills. The returned descriptor is duplicated to
    the peer by the server, which closes its own copy afterwards.
    """
    checked = _validate_path(path)
    _require_compose_file(checked)
    argv = [_DOCKER, "compose", "--project-directory", checked, *_VERB_ARGS[verb]]
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            argv,
            shell=False,
            stdout=write_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise ActionError(HELPER_DOCKER_UNAVAILABLE, "docker CLI not found") from exc
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise ActionError(HELPER_INTERNAL, f"failed to start docker compose {verb}: {exc}") from exc

    def _reap() -> None:
        # The child holds its own duplicate of write_fd; keep ours open so the
        # marker can be appended after every child byte reached the pipe.
        try:
            code = proc.wait()
        except Exception:  # the reaper thread must never crash the helper
            code = -1
        with suppress(OSError):
            os.write(write_fd, f"{EXIT_MARKER}{code}\n".encode())
        with suppress(OSError):
            os.close(write_fd)

    threading.Thread(target=_reap, name=f"compose-{verb}-{proc.pid}", daemon=True).start()
    return ActionOutcome(result={"pid": proc.pid}, fds=[read_fd])


@register("compose.up", ProjectArgs)
def compose_up(args: ProjectArgs) -> ActionOutcome:
    """``docker compose up -d --remove-orphans``; output streams via the fd."""
    return _compose_stream(args.path, "up")


@register("compose.down", ProjectArgs)
def compose_down(args: ProjectArgs) -> ActionOutcome:
    """``docker compose down``; output streams via the fd."""
    return _compose_stream(args.path, "down")


@register("compose.restart", ProjectArgs)
def compose_restart(args: ProjectArgs) -> ActionOutcome:
    """``docker compose restart``; output streams via the fd."""
    return _compose_stream(args.path, "restart")
