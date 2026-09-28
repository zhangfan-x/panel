"""Helper docker engine actions via docker-py (docs/code-plan-m2-m5.md §3.4).

The docker client is created lazily inside handlers (``_client()`` caches a
``docker.from_env()`` singleton) so importing this module never touches the
daemon. A daemon that is down or unreachable surfaces as the stable
``helper.docker_unavailable`` code — except for ``docker.info`` which reports
``{"available": false}`` instead of failing. Handlers self-register via
``@register`` at import time; ``helper/actions/__init__.py`` imports this module
on helper startup.
"""

from __future__ import annotations

import json
import os
import select
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from typing import Any, Literal

import docker  # type: ignore[import-untyped]
from docker.errors import (  # type: ignore[import-untyped]
    APIError,
    DockerException,
    NotFound,
)
from pydantic import BaseModel, Field

from ownpanel.helper.registry import ActionError, ActionOutcome, HandlerResult, register
from ownpanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_DENIED,
    HELPER_DOCKER_UNAVAILABLE,
    HELPER_INTERNAL,
    HELPER_INVALID_ARGS,
    HELPER_NOT_FOUND,
    HELPER_UNSUPPORTED,
)

# Argument shapes (keep in sync with the API-side copies in dockermgr/router.py).
# Container/network/volume identifiers: engine ids or names, no path/shell noise.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
# Image references may additionally carry registry host, port, path and digest.
IMAGE_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,254}$"
REPOSITORY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$"
TAG_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"

# Byte budget for the JSON-encoded snapshot log payload; the whole response
# frame must stay under ipc.framing.MAX_FRAME_BYTES (256 KiB).
_SNAPSHOT_BUDGET = 128 * 1024

_client_lock = threading.Lock()
_docker_client: Any = None


def _client() -> Any:
    """Lazily create and cache the docker client; never runs at import time.

    Raises:
        ActionError: ``helper.docker_unavailable`` when the daemon cannot be
            reached while creating the client.
    """
    global _docker_client
    with _client_lock:
        if _docker_client is None:
            try:
                _docker_client = docker.from_env()
            except (DockerException, OSError) as exc:
                raise ActionError(
                    HELPER_DOCKER_UNAVAILABLE, f"docker daemon unavailable: {exc}"
                ) from exc
        return _docker_client


@contextmanager
def _map_docker_errors(what: str) -> Iterator[None]:
    """Translate docker-py errors into stable helper error codes."""
    try:
        yield
    except NotFound as exc:
        raise ActionError(HELPER_NOT_FOUND, f"not found: {what}") from exc
    except APIError as exc:
        status = int(exc.status_code or 500)
        message = str(exc.explanation or exc)
        if status == 409:
            raise ActionError(HELPER_CONFLICT, message) from exc
        if status == 400:
            raise ActionError(HELPER_INVALID_ARGS, message) from exc
        if status == 403:
            raise ActionError(HELPER_DENIED, message) from exc
        if status in (405, 501):
            raise ActionError(HELPER_UNSUPPORTED, message) from exc
        raise ActionError(HELPER_INTERNAL, message) from exc
    except (DockerException, OSError) as exc:
        raise ActionError(HELPER_DOCKER_UNAVAILABLE, f"docker daemon unavailable: {exc}") from exc


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


class InfoArgs(BaseModel):
    pass


@register("docker.info", InfoArgs)
def docker_info(args: InfoArgs) -> dict[str, Any]:
    """Daemon liveness and summary; reports availability instead of raising."""
    try:
        client = _client()
        info: dict[str, Any] = client.info()
        version: dict[str, Any] = client.version()
    except (ActionError, DockerException, OSError):
        return {"available": False}
    return {
        "available": True,
        "version": str(info.get("ServerVersion", "")),
        "api_version": str(version.get("ApiVersion", "")),
        "os": str(info.get("OperatingSystem", "")),
        "kernel": str(info.get("KernelVersion", "")),
        "arch": str(info.get("Architecture", "")),
        "containers": int(info.get("Containers", 0)),
        "running": int(info.get("ContainersRunning", 0)),
        "paused": int(info.get("ContainersPaused", 0)),
        "stopped": int(info.get("ContainersStopped", 0)),
        "images": int(info.get("Images", 0)),
    }


# ---------------------------------------------------------------------------
# containers
# ---------------------------------------------------------------------------


class ContainerListArgs(BaseModel):
    all: bool = False


def _container_row(summary: dict[str, Any]) -> dict[str, Any]:
    names = summary.get("Names") or []
    container_id = str(summary.get("Id", ""))
    name = str(names[0]).lstrip("/") if names else container_id[:12]
    return {
        "id": container_id,
        "name": name,
        "image": str(summary.get("Image", "")),
        "state": str(summary.get("State", "")),
        "status": str(summary.get("Status", "")),
        "ports": summary.get("Ports") or [],
        "created": int(summary.get("Created", 0)),
        "labels": summary.get("Labels") or {},
    }


@register("docker.container_list", ContainerListArgs)
def container_list(args: ContainerListArgs) -> dict[str, Any]:
    """Container summaries straight from the engine's list endpoint."""
    client = _client()
    with _map_docker_errors("containers"):
        summaries: list[dict[str, Any]] = client.api.containers(all=args.all)
    return {"containers": [_container_row(s) for s in summaries]}


class ContainerRefArgs(BaseModel):
    id: str = Field(pattern=NAME_PATTERN)


@register("docker.container_inspect", ContainerRefArgs)
def container_inspect(args: ContainerRefArgs) -> dict[str, Any]:
    """Inspect one container, trimmed down to the fields the panel displays."""
    client = _client()
    with _map_docker_errors(f"container {args.id}"):
        attrs: dict[str, Any] = client.api.inspect_container(args.id)
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    host_config = attrs.get("HostConfig") or {}
    net = attrs.get("NetworkSettings") or {}
    health = state.get("Health") or {}
    return {
        "id": str(attrs.get("Id", "")),
        "name": str(attrs.get("Name", "")).lstrip("/"),
        "image": str(config.get("Image", "")),
        "image_id": str(attrs.get("Image", "")),
        "created": str(attrs.get("Created", "")),
        "state": {
            "status": str(state.get("Status", "")),
            "running": bool(state.get("Running", False)),
            "paused": bool(state.get("Paused", False)),
            "exit_code": int(state.get("ExitCode", 0)),
            "started_at": str(state.get("StartedAt", "")),
            "finished_at": str(state.get("FinishedAt", "")),
            "error": str(state.get("Error", "")),
            "pid": int(state.get("Pid", 0)),
            "health": str(health.get("Status", "")) or None,
        },
        "restart_policy": str((host_config.get("RestartPolicy") or {}).get("Name", "")),
        "cmd": config.get("Cmd"),
        "entrypoint": config.get("Entrypoint"),
        "env": config.get("Env") or [],
        "labels": config.get("Labels") or {},
        "ports": net.get("Ports") or {},
        "networks": {
            net_name: {
                "ip_address": str((net_info or {}).get("IPAddress", "")),
                "gateway": str((net_info or {}).get("Gateway", "")),
                "mac_address": str((net_info or {}).get("MacAddress", "")),
            }
            for net_name, net_info in (net.get("Networks") or {}).items()
        },
        "mounts": [
            {
                "type": str(m.get("Type", "")),
                "source": str(m.get("Source", "")),
                "destination": str(m.get("Destination", "")),
                "rw": bool(m.get("RW", True)),
            }
            for m in attrs.get("Mounts") or []
        ],
    }


ContainerVerb = Literal["start", "stop", "restart", "pause", "unpause", "kill", "remove"]


class ContainerActionArgs(BaseModel):
    id: str = Field(pattern=NAME_PATTERN)
    action: ContainerVerb
    force: bool = False


@register("docker.container_action", ContainerActionArgs)
def container_action(args: ContainerActionArgs) -> dict[str, Any]:
    """Run one lifecycle verb against a container (``force`` applies to remove)."""
    client = _client()
    with _map_docker_errors(f"container {args.id}"):
        container = client.containers.get(args.id)
        if args.action == "remove":
            container.remove(force=args.force)
        else:
            getattr(container, args.action)()
    return {"ok": True}


class LogsArgs(BaseModel):
    id: str = Field(pattern=NAME_PATTERN)
    tail: int = Field(default=200, ge=1, le=2000)
    follow: bool = False


def _tail_lines_within_budget(lines: list[str], budget: int) -> tuple[list[str], bool]:
    """Keep the newest lines whose JSON-encoded size fits within ``budget``."""
    total = 0
    kept: list[str] = []
    for line in reversed(lines):
        cost = len(json.dumps(line, ensure_ascii=False).encode("utf-8")) + 1
        if total + cost > budget:
            return list(reversed(kept)), True
        kept.append(line)
        total += cost
    return list(reversed(kept)), False


def _pump_stream(stream: Any, write_fd: int) -> None:
    """Copy a blocking docker log stream into a pipe until EOF or reader close."""
    try:
        for chunk in stream:
            if not chunk:
                continue
            view = memoryview(chunk)
            while view:
                written = os.write(write_fd, view)
                view = view[written:]
    except Exception:
        # Reader closed its end (EPIPE) or the engine went away: stop pumping.
        pass
    finally:
        _unregister_follower(write_fd)
        with suppress(Exception):
            stream.close()
        with suppress(OSError):
            os.close(write_fd)


# Track live log-follow pumps so an abandoned follow (client disconnected from a
# silent container, where the pump is blocked in a read that never returns) can
# be reaped: when its pipe read-end is closed, poll() reports POLLERR on the
# write-end, and closing the docker stream unblocks the pump thread.
_FOLLOWERS: dict[int, Any] = {}
_FOLLOWERS_LOCK = threading.Lock()


def _register_follower(write_fd: int, stream: Any) -> None:
    with _FOLLOWERS_LOCK:
        _FOLLOWERS[write_fd] = stream


def _unregister_follower(write_fd: int) -> None:
    with _FOLLOWERS_LOCK:
        _FOLLOWERS.pop(write_fd, None)


def _reap_followers() -> None:
    """Close docker streams whose pipe reader has gone away (unblocks their pump)."""
    dead_mask = select.POLLERR | select.POLLHUP | select.POLLNVAL
    with _FOLLOWERS_LOCK:
        for write_fd, stream in list(_FOLLOWERS.items()):
            try:
                poller = select.poll()
                poller.register(write_fd, select.POLLERR)
                dead = any(ev & dead_mask for _fd, ev in poller.poll(0))
            except OSError:
                dead = True
            if dead:
                with suppress(Exception):
                    stream.close()


@register("docker.logs", LogsArgs)
def logs(args: LogsArgs) -> HandlerResult:
    """Container logs: snapshot of the last ``tail`` lines, or a follow pipe fd.

    With ``follow`` a daemon thread copies the engine's streaming log feed into
    an ``os.pipe`` whose read end rides on the response; the API bridges it to
    SSE and simply closes its descriptor to stop the follow.
    """
    client = _client()
    with _map_docker_errors(f"container {args.id}"):
        container = client.containers.get(args.id)
        if not args.follow:
            raw: bytes = container.logs(tail=args.tail, stream=False, follow=False)
            all_lines = raw.decode("utf-8", "replace").splitlines()
            lines, truncated = _tail_lines_within_budget(all_lines, _SNAPSHOT_BUDGET)
            return {"lines": lines, "truncated": truncated}
        stream = container.logs(stream=True, follow=True, tail=args.tail)
    _reap_followers()
    read_fd, write_fd = os.pipe()
    _register_follower(write_fd, stream)
    threading.Thread(
        target=_pump_stream,
        args=(stream, write_fd),
        name=f"docker-logs-{args.id[:12]}",
        daemon=True,
    ).start()
    return ActionOutcome(result={"follow": True}, fds=[read_fd])


class ExecCreateArgs(BaseModel):
    id: str = Field(pattern=NAME_PATTERN)
    cmd: list[str] = Field(min_length=1, max_length=64)
    tty: bool = True


@register("docker.exec_create", ExecCreateArgs)
def exec_create(args: ExecCreateArgs) -> ActionOutcome:
    """Create and start an interactive exec; returns the hijacked socket fd.

    The duplicated descriptor is bidirectional (stdin/stdout of the exec'd
    process); the API side bridges it to a WebSocket. docker-py's own socket
    object is closed here — the dup keeps the connection alive until the last
    descriptor (the peer's SCM_RIGHTS copy) is closed.
    """
    client = _client()
    with _map_docker_errors(f"container {args.id}"):
        created: dict[str, Any] = client.api.exec_create(
            args.id, args.cmd, tty=args.tty, stdin=True, stdout=True, stderr=True
        )
        exec_id = str(created["Id"])
        sock = client.api.exec_start(exec_id, socket=True, tty=args.tty)
    fd = os.dup(sock.fileno())
    with suppress(Exception):
        sock.close()
    return ActionOutcome(result={"exec_id": exec_id}, fds=[fd])


# Exec ids are 64-char hex digests, but accept a slightly wider alphanumeric
# band so a future engine id format does not spuriously fail validation.
EXEC_ID_PATTERN = r"^[A-Za-z0-9]{16,128}$"


class ExecResizeArgs(BaseModel):
    exec_id: str = Field(pattern=EXEC_ID_PATTERN)
    height: int = Field(ge=1, le=1000)
    width: int = Field(ge=1, le=1000)


@register("docker.exec_resize", ExecResizeArgs)
def exec_resize(args: ExecResizeArgs) -> dict[str, Any]:
    """Resize an interactive exec's TTY (``{"type":"resize"}`` control frames)."""
    client = _client()
    with _map_docker_errors(f"exec {args.exec_id}"):
        client.api.exec_resize(args.exec_id, height=args.height, width=args.width)
    return {"ok": True}


# ---------------------------------------------------------------------------
# stats (one-shot resource snapshot)
# ---------------------------------------------------------------------------


class StatsArgs(BaseModel):
    id: str = Field(pattern=NAME_PATTERN)


def _dig(source: Any, *keys: str) -> Any:
    """Walk nested dicts defensively; return None on any missing/typed-out step."""
    cur = source
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _compute_stats(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a docker one-shot ``stats`` dict to a flat, JSON-safe snapshot.

    CPU percent uses the engine's own formula (delta container / delta system *
    online cpus * 100); everything is guarded so a stopped container (empty
    ``cpu_stats``/``precpu_stats``) yields zeros instead of a ZeroDivisionError.
    Memory ``used`` subtracts the inactive file cache (cgroup v2 ``inactive_file``
    or v1 ``total_inactive_file``) the way ``docker stats`` does.
    """
    cpu_total = _as_int(_dig(raw, "cpu_stats", "cpu_usage", "total_usage"))
    precpu_total = _as_int(_dig(raw, "precpu_stats", "cpu_usage", "total_usage"))
    system = _as_int(_dig(raw, "cpu_stats", "system_cpu_usage"))
    presystem = _as_int(_dig(raw, "precpu_stats", "system_cpu_usage"))
    online = _as_int(_dig(raw, "cpu_stats", "online_cpus"))
    if online <= 0:
        percpu = _dig(raw, "cpu_stats", "cpu_usage", "percpu_usage")
        online = len(percpu) if isinstance(percpu, list) else 0
    cpu_delta = cpu_total - precpu_total
    system_delta = system - presystem
    cpu_percent = 0.0
    if system_delta > 0 and cpu_delta > 0:
        cpu_percent = (cpu_delta / system_delta) * (online or 1) * 100.0

    mem_usage = _as_int(_dig(raw, "memory_stats", "usage"))
    cache = _dig(raw, "memory_stats", "stats", "inactive_file")
    if cache is None:
        cache = _dig(raw, "memory_stats", "stats", "total_inactive_file")
    mem_used = mem_usage - _as_int(cache)
    if mem_used < 0:
        mem_used = mem_usage
    mem_limit = _as_int(_dig(raw, "memory_stats", "limit"))
    mem_percent = (mem_used / mem_limit * 100.0) if mem_limit > 0 else 0.0

    net_rx = 0
    net_tx = 0
    networks = raw.get("networks")
    if isinstance(networks, dict):
        for iface in networks.values():
            net_rx += _as_int(_dig(iface, "rx_bytes"))
            net_tx += _as_int(_dig(iface, "tx_bytes"))

    blk_read = 0
    blk_write = 0
    entries = _dig(raw, "blkio_stats", "io_service_bytes_recursive")
    if isinstance(entries, list):
        for entry in entries:
            op = str(_dig(entry, "op") or "").lower()
            if op == "read":
                blk_read += _as_int(_dig(entry, "value"))
            elif op == "write":
                blk_write += _as_int(_dig(entry, "value"))

    return {
        "read": str(raw.get("read", "")),
        "cpu_percent": round(cpu_percent, 2),
        "online_cpus": online,
        "mem_used": mem_used,
        "mem_limit": mem_limit,
        "mem_percent": round(mem_percent, 2),
        "net_rx": net_rx,
        "net_tx": net_tx,
        "blk_read": blk_read,
        "blk_write": blk_write,
        "pids": _as_int(_dig(raw, "pids_stats", "current")),
    }


@register("docker.stats", StatsArgs)
def stats(args: StatsArgs) -> dict[str, Any]:
    """One-shot resource stats for a container (CPU%/mem/net/blk/pids)."""
    client = _client()
    with _map_docker_errors(f"container {args.id}"):
        container = client.containers.get(args.id)
        raw: dict[str, Any] = container.stats(stream=False)
    return _compute_stats(raw)


class StatsAllArgs(BaseModel):
    pass


@register("docker.stats_all", StatsAllArgs)
def stats_all(args: StatsAllArgs) -> dict[str, Any]:
    """One-shot stats for every running container, keyed by full container id.

    Each ``stats(stream=False)`` blocks ~1s (the daemon takes two samples for
    the CPU delta), so containers are sampled in parallel on a small thread
    pool. A container that disappears mid-sample (NotFound/APIError race) is
    skipped instead of failing the whole snapshot. Frame budget: one snapshot
    is ~300 bytes of JSON, so even hundreds of containers stay far below
    ipc.framing.MAX_FRAME_BYTES (256 KiB).
    """
    client = _client()
    with _map_docker_errors("containers"):
        containers: list[Any] = client.containers.list()
    stats_map: dict[str, Any] = {}
    if not containers:
        return {"stats": stats_map}

    def _sample(container: Any) -> tuple[str, dict[str, Any]] | None:
        try:
            raw: dict[str, Any] = container.stats(stream=False)
        except (DockerException, OSError):
            return None
        return str(container.id), _compute_stats(raw)

    max_workers = min(8, max(1, len(containers)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for sampled in pool.map(_sample, containers):
            if sampled is not None:
                stats_map[sampled[0]] = sampled[1]
    return {"stats": stats_map}


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


class ImageListArgs(BaseModel):
    pass


@register("docker.image_list", ImageListArgs)
def image_list(args: ImageListArgs) -> dict[str, Any]:
    """Image summaries: id, tags, size and creation time."""
    client = _client()
    with _map_docker_errors("images"):
        summaries: list[dict[str, Any]] = client.api.images()
    images = [
        {
            "id": str(s.get("Id", "")),
            "tags": [t for t in (s.get("RepoTags") or []) if t != "<none>:<none>"],
            "size": int(s.get("Size", 0)),
            "created": int(s.get("Created", 0)),
        }
        for s in summaries
    ]
    return {"images": images}


class ImagePullArgs(BaseModel):
    repository: str = Field(pattern=REPOSITORY_PATTERN)
    tag: str = Field(default="latest", pattern=TAG_PATTERN)


def _split_repository(repository: str, default_tag: str) -> tuple[str, str]:
    """Split an embedded ``:tag`` off the repository (docker-py semantics)."""
    head, sep, tail = repository.rpartition(":")
    if sep and "/" not in tail:
        return head, tail
    return repository, default_tag


@register("docker.image_pull", ImagePullArgs)
def image_pull(args: ImagePullArgs) -> dict[str, Any]:
    """Blocking image pull; consumes the progress stream and returns a summary.

    The API wraps this action in a background job — in-stream registry errors
    are mapped to stable codes instead of a generic failure.
    """
    client = _client()
    repository, tag = _split_repository(args.repository, args.tag)
    ref = f"{repository}:{tag}"
    layers: set[str] = set()
    last_status = ""
    with _map_docker_errors(f"image {ref}"):
        for entry in client.api.pull(repository, tag=tag, stream=True, decode=True):
            error = entry.get("error")
            if error:
                message = str(error)
                lowered = message.lower()
                code = (
                    HELPER_NOT_FOUND
                    if "not found" in lowered or "manifest unknown" in lowered
                    else HELPER_INTERNAL
                )
                raise ActionError(code, message)
            if entry.get("id"):
                layers.add(str(entry["id"]))
            if entry.get("status"):
                last_status = str(entry["status"])
        image_id = str(client.images.get(ref).id)
    return {
        "repository": repository,
        "tag": tag,
        "id": image_id,
        "layers": len(layers),
        "status": last_status,
    }


class ImageRemoveArgs(BaseModel):
    id: str = Field(pattern=IMAGE_REF_PATTERN)
    force: bool = False


@register("docker.image_remove", ImageRemoveArgs)
def image_remove(args: ImageRemoveArgs) -> dict[str, Any]:
    """Remove an image by id or reference (conflict when still in use)."""
    client = _client()
    with _map_docker_errors(f"image {args.id}"):
        client.images.remove(image=args.id, force=args.force)
    return {"ok": True}


# ---------------------------------------------------------------------------
# networks
# ---------------------------------------------------------------------------


class NetworkListArgs(BaseModel):
    pass


@register("docker.network_list", NetworkListArgs)
def network_list(args: NetworkListArgs) -> dict[str, Any]:
    """Network summaries."""
    client = _client()
    with _map_docker_errors("networks"):
        summaries: list[dict[str, Any]] = client.api.networks()
    networks = [
        {
            "id": str(s.get("Id", "")),
            "name": str(s.get("Name", "")),
            "driver": str(s.get("Driver", "")),
            "scope": str(s.get("Scope", "")),
            "internal": bool(s.get("Internal", False)),
            "created": str(s.get("Created", "")),
        }
        for s in summaries
    ]
    return {"networks": networks}


class NetworkCreateArgs(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    driver: Literal["bridge", "overlay", "macvlan", "ipvlan"] = "bridge"
    internal: bool = False


@register("docker.network_create", NetworkCreateArgs)
def network_create(args: NetworkCreateArgs) -> dict[str, Any]:
    """Create a user-defined network."""
    client = _client()
    with _map_docker_errors(f"network {args.name}"):
        network = client.networks.create(args.name, driver=args.driver, internal=args.internal)
    return {"id": str(network.id), "name": args.name}


class NetworkRemoveArgs(BaseModel):
    id: str = Field(pattern=NAME_PATTERN)


@register("docker.network_remove", NetworkRemoveArgs)
def network_remove(args: NetworkRemoveArgs) -> dict[str, Any]:
    """Remove a network by id or name (built-ins are denied by the engine)."""
    client = _client()
    with _map_docker_errors(f"network {args.id}"):
        client.api.remove_network(args.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# volumes
# ---------------------------------------------------------------------------


class VolumeListArgs(BaseModel):
    pass


@register("docker.volume_list", VolumeListArgs)
def volume_list(args: VolumeListArgs) -> dict[str, Any]:
    """Volume summaries."""
    client = _client()
    with _map_docker_errors("volumes"):
        response: dict[str, Any] = client.api.volumes()
    volumes = [
        {
            "name": str(v.get("Name", "")),
            "driver": str(v.get("Driver", "")),
            "mountpoint": str(v.get("Mountpoint", "")),
            "created": str(v.get("CreatedAt", "")),
            "scope": str(v.get("Scope", "")),
            "labels": v.get("Labels") or {},
        }
        for v in response.get("Volumes") or []
    ]
    return {"volumes": volumes}


class VolumeCreateArgs(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)


@register("docker.volume_create", VolumeCreateArgs)
def volume_create(args: VolumeCreateArgs) -> dict[str, Any]:
    """Create a named volume."""
    client = _client()
    with _map_docker_errors(f"volume {args.name}"):
        volume = client.volumes.create(name=args.name)
    return {"name": str(volume.name)}


class VolumeRemoveArgs(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    force: bool = False


@register("docker.volume_remove", VolumeRemoveArgs)
def volume_remove(args: VolumeRemoveArgs) -> dict[str, Any]:
    """Remove a volume (conflict while a container still uses it)."""
    client = _client()
    with _map_docker_errors(f"volume {args.name}"):
        client.api.remove_volume(args.name, force=args.force)
    return {"ok": True}
