"""Async client and pure parsers for the Clash/Mihomo external controller.

The controller is a localhost RESTful API (``settings.proxy.clash_api``),
optionally protected by a Bearer token (``settings.proxy.clash_secret``). This
module wraps the handful of endpoints the proxy router needs and keeps the
payload-shaping logic in pure functions (:func:`parse_status`,
:func:`parse_groups`) so they are unit-testable without a live controller.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

#: Default latency-probe target (Clash's conventional generate_204 endpoint).
DELAY_TEST_URL = "http://www.gstatic.com/generate_204"

#: Proxy "types" that represent selectable/aggregate groups (they carry ``all``).
GROUP_TYPES = frozenset({"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"})

_DEFAULT_TIMEOUT_S = 10.0

#: Extra read budget on top of the probe timeout for a whole-group delay test.
_GROUP_DELAY_GRACE_S = 30.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _latest_delay(info: dict[str, Any]) -> int:
    """Most recent recorded latency (ms) for a proxy; ``0`` when unknown/timeout."""
    history = info.get("history")
    if isinstance(history, list) and history:
        last = history[-1]
        if isinstance(last, dict):
            return _as_int(last.get("delay"))
    return 0


def _resolve_delay(
    table: dict[str, Any], name: str, seen: frozenset[str] = frozenset()
) -> tuple[int, str]:
    """Latency for ``name``, following group ``now`` pointers when it has none.

    Mihomo never records history on group-type proxies (Selector, URLTest…),
    only on real outbounds — so a group nested inside another group would
    otherwise always report ``0`` and render as "—". Resolving through ``now``
    surfaces the latency of the node the group currently routes through.

    Returns ``(delay_ms, source_name)``; ``source_name`` differs from ``name``
    only when the value was inherited, and is ``""`` when nothing was found.
    ``seen`` guards against cyclic ``now`` chains.
    """
    info = table.get(name)
    if not isinstance(info, dict) or name in seen:
        return 0, ""
    delay = _latest_delay(info)
    if delay:
        return delay, name
    now = _as_str(info.get("now"))
    if _as_str(info.get("type")) in GROUP_TYPES and now:
        return _resolve_delay(table, now, seen | {name})
    return 0, ""


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    """Best-effort JSON-object decode of a response (``{}`` on any failure)."""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_status(version: dict[str, Any], configs: dict[str, Any]) -> dict[str, Any]:
    """Combine ``/version`` + ``/configs`` into the ``GET /proxy/status`` body."""
    return {
        "version": _as_str(version.get("version")),
        "mode": _as_str(configs.get("mode")),
        "mixed_port": _as_int(configs.get("mixed-port")),
        "port": _as_int(configs.get("port")),
        "socks_port": _as_int(configs.get("socks-port")),
        "redir_port": _as_int(configs.get("redir-port")),
        "allow_lan": bool(configs.get("allow-lan")),
        "log_level": _as_str(configs.get("log-level")),
    }


def parse_groups(proxies: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract selectable groups from a ``GET /proxies`` payload.

    Returns one entry per group proxy (``type`` in :data:`GROUP_TYPES`), each
    shaped ``{name, type, now, all: [{name, type, delay, delay_of}]}`` where
    ``delay`` is the latest recorded latency in milliseconds (``0`` =
    unknown/timeout). ``delay_of`` names the proxy the value was inherited from
    when the member is itself a group (empty otherwise), so the UI can tell an
    inherited latency apart from a measured one.
    """
    table = proxies.get("proxies")
    if not isinstance(table, dict):
        return []
    groups: list[dict[str, Any]] = []
    for name, info in table.items():
        if not isinstance(info, dict):
            continue
        ptype = _as_str(info.get("type"))
        members = info.get("all")
        if ptype not in GROUP_TYPES or not isinstance(members, list):
            continue
        nodes: list[dict[str, Any]] = []
        for member in members:
            if not isinstance(member, str):
                continue
            node = table.get(member)
            node_info = node if isinstance(node, dict) else {}
            delay, source = _resolve_delay(table, member)
            nodes.append(
                {
                    "name": member,
                    "type": _as_str(node_info.get("type")),
                    "delay": delay,
                    "delay_of": "" if source == member else source,
                }
            )
        groups.append(
            {
                "name": _as_str(name),
                "type": ptype,
                "now": _as_str(info.get("now")),
                "all": nodes,
            }
        )
    return groups


class ClashClient:
    """Thin async wrapper over the Clash/Mihomo external controller.

    All requests are localhost HTTP. A ``secret`` (non-empty) is sent as a
    Bearer token. Callers own the lifecycle: use ``async with ClashClient(...)``
    or call :meth:`aclose` in a ``finally``.
    """

    def __init__(
        self,
        base_url: str,
        secret: str = "",
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ClashClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # —— reads ——

    async def version(self) -> dict[str, Any]:
        resp = await self._client.get("/version")
        resp.raise_for_status()
        return _json_object(resp)

    async def configs(self) -> dict[str, Any]:
        resp = await self._client.get("/configs")
        resp.raise_for_status()
        return _json_object(resp)

    async def proxies(self) -> dict[str, Any]:
        resp = await self._client.get("/proxies")
        resp.raise_for_status()
        return _json_object(resp)

    async def proxy(self, name: str) -> dict[str, Any]:
        resp = await self._client.get(f"/proxies/{quote(name, safe='')}")
        resp.raise_for_status()
        return _json_object(resp)

    async def rules(self) -> dict[str, Any]:
        resp = await self._client.get("/rules")
        resp.raise_for_status()
        return _json_object(resp)

    async def connections(self) -> dict[str, Any]:
        resp = await self._client.get("/connections")
        resp.raise_for_status()
        return _json_object(resp)

    async def delay(
        self, name: str, *, timeout_ms: int = 5000, url: str = DELAY_TEST_URL
    ) -> dict[str, Any]:
        """Latency probe for one proxy: ``{delay}`` (0 = timeout, with ``message``)."""
        resp = await self._client.get(
            f"/proxies/{quote(name, safe='')}/delay",
            params={"timeout": timeout_ms, "url": url},
        )
        data = _json_object(resp)
        if resp.is_success:
            return {"delay": _as_int(data.get("delay"))}
        return {"delay": 0, "message": _as_str(data.get("message"))}

    async def group_delay(
        self, group: str, *, timeout_ms: int = 5000, url: str = DELAY_TEST_URL
    ) -> dict[str, int] | None:
        """Probe every member of ``group`` in one controller call.

        Mihomo's ``GET /group/{name}/delay`` answers ``{node: delay_ms}`` for the
        whole group, replacing one request per member. Returns ``None`` when the
        controller does not implement the endpoint (older Clash cores answer
        404), so callers can fall back to per-node probes. Members that failed
        the probe are simply absent from the controller's map.
        """
        # The controller probes members concurrently, but a large group still
        # needs noticeably longer than the per-probe timeout; allow a bounded
        # grace period on top rather than the client-wide default.
        read_timeout = timeout_ms / 1000.0 + _GROUP_DELAY_GRACE_S
        resp = await self._client.get(
            f"/group/{quote(group, safe='')}/delay",
            params={"timeout": timeout_ms, "url": url},
            timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return {
            name: _as_int(value)
            for name, value in _json_object(resp).items()
            if isinstance(name, str)
        }

    # —— mutations ——

    async def patch_configs(self, payload: dict[str, Any]) -> None:
        resp = await self._client.patch("/configs", json=payload)
        resp.raise_for_status()

    async def reload_configs(self, *, path: str) -> None:
        resp = await self._client.put("/configs", params={"force": "true"}, json={"path": path})
        resp.raise_for_status()

    async def select(self, group: str, name: str) -> None:
        resp = await self._client.put(f"/proxies/{quote(group, safe='')}", json={"name": name})
        resp.raise_for_status()

    async def close_connections(self) -> None:
        resp = await self._client.delete("/connections")
        resp.raise_for_status()

    async def close_connection(self, conn_id: str) -> None:
        """Close one connection by its controller-assigned id."""
        resp = await self._client.delete(f"/connections/{quote(conn_id, safe='')}")
        resp.raise_for_status()

    # —— streaming ——

    @contextlib.asynccontextmanager
    async def stream_traffic(self) -> AsyncIterator[AsyncIterator[str]]:
        """Open ``GET /traffic`` and yield an iterator of raw newline-delimited JSON lines.

        The read timeout is disabled for the lifetime of the stream (the
        controller emits one line per second, indefinitely).
        """
        timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
        async with self._client.stream("GET", "/traffic", timeout=timeout) as resp:
            resp.raise_for_status()
            yield resp.aiter_lines()
