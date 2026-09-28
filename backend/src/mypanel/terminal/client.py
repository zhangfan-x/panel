"""API-side client for the ``mypanel-terminal`` PTY service socket (code-plan-m2-m5 §5.2).

One fresh ``AF_UNIX`` connection per call (mirrors :class:`mypanel.ipc.client.HelperClient`).
Connection/timeout/protocol trouble raises :class:`mypanel.errors.TerminalUnavailableError`
(HTTP 503); a business failure reported by the service raises
:class:`TerminalServiceError`. ``open`` returns the PTY master fd and the caller
owns it (must ``close`` it).
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mypanel.errors import TerminalUnavailableError
from mypanel.ipc.fdpass import close_fds
from mypanel.ipc.framing import FramingError, recv_frame, send_frame
from mypanel.ipc.messages import HelperRequest, HelperResponse

_OPEN_TIMEOUT_S = 10.0
_CONTROL_TIMEOUT_S = 5.0


class TerminalServiceError(Exception):
    """The terminal service reported a failure; carries its wire error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TerminalClient:
    """Client for the root terminal service Unix socket."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    def _exchange(
        self, action: str, args: dict[str, Any] | None, *, timeout_s: float
    ) -> tuple[dict[str, Any], list[int]]:
        request = HelperRequest(
            id=str(uuid.uuid4()),
            action=action,
            args=dict(args) if args else {},
            timeout_s=timeout_s,
        )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout_s)
                sock.connect(str(self._socket_path))
                send_frame(sock, request.model_dump())
                payload, fds = recv_frame(sock)
        except (OSError, FramingError) as exc:
            raise TerminalUnavailableError(f"terminal service unavailable: {exc}") from exc
        try:
            response = HelperResponse.model_validate(payload)
        except ValidationError as exc:
            close_fds(fds)
            raise TerminalUnavailableError("terminal response failed validation") from exc
        if response.id != request.id:
            close_fds(fds)
            raise TerminalUnavailableError("terminal response id mismatch")
        if not response.ok:
            close_fds(fds)
            if response.error is None:
                raise TerminalUnavailableError("terminal failure response lacks error detail")
            raise TerminalServiceError(response.error.code, response.error.message)
        if response.fds != len(fds):
            close_fds(fds)
            raise TerminalUnavailableError(
                f"terminal declared {response.fds} fds but delivered {len(fds)}"
            )
        return response.result or {}, fds

    def ping(self, *, timeout_s: float = _CONTROL_TIMEOUT_S) -> None:
        """Reachability + peer-UID probe; raises TerminalUnavailableError if unreachable."""
        _result, fds = self._exchange("terminal.ping", None, timeout_s=timeout_s)
        close_fds(fds)

    def open(self, cols: int, rows: int, *, timeout_s: float = _OPEN_TIMEOUT_S) -> tuple[int, int]:
        """Open a PTY; returns (pid, master_fd). The caller owns ``master_fd``."""
        result, fds = self._exchange(
            "terminal.open", {"cols": cols, "rows": rows}, timeout_s=timeout_s
        )
        pid = result.get("pid")
        if len(fds) != 1 or not isinstance(pid, int):
            close_fds(fds)
            raise TerminalUnavailableError("terminal.open returned an unexpected response")
        return pid, fds[0]

    def close(self, pid: int, *, timeout_s: float = _CONTROL_TIMEOUT_S) -> int | None:
        result, fds = self._exchange("terminal.close", {"pid": pid}, timeout_s=timeout_s)
        close_fds(fds)
        status = result.get("exit_status")
        return status if isinstance(status, int) else None

    def resize(
        self, pid: int, cols: int, rows: int, *, timeout_s: float = _CONTROL_TIMEOUT_S
    ) -> None:
        _result, fds = self._exchange(
            "terminal.resize", {"pid": pid, "cols": cols, "rows": rows}, timeout_s=timeout_s
        )
        close_fds(fds)

    async def aping(self, *, timeout_s: float = _CONTROL_TIMEOUT_S) -> None:
        await asyncio.to_thread(self.ping, timeout_s=timeout_s)

    async def aopen(
        self, cols: int, rows: int, *, timeout_s: float = _OPEN_TIMEOUT_S
    ) -> tuple[int, int]:
        return await asyncio.to_thread(self.open, cols, rows, timeout_s=timeout_s)

    async def aclose(self, pid: int, *, timeout_s: float = _CONTROL_TIMEOUT_S) -> int | None:
        return await asyncio.to_thread(self.close, pid, timeout_s=timeout_s)

    async def aresize(
        self, pid: int, cols: int, rows: int, *, timeout_s: float = _CONTROL_TIMEOUT_S
    ) -> None:
        await asyncio.to_thread(self.resize, pid, cols, rows, timeout_s=timeout_s)
