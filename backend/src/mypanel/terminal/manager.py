"""API-side terminal session coordinator (code-plan-m2-m5 §5.2).

Holds the single-active-session invariant and one-time WebSocket tickets in
memory; only audit metadata is persisted (``terminal_sessions``). This module
also owns the connect/bridge machinery: it talks to the root ``mypanel-terminal``
service over :class:`~mypanel.terminal.client.TerminalClient` and bridges the PTY
master fd to a WebSocket (binary frames are PTY I/O, JSON text frames are control).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from starlette.websockets import WebSocket, WebSocketDisconnect

from mypanel.config import Settings
from mypanel.errors import ConflictError, TerminalUnavailableError
from mypanel.logging import get_logger
from mypanel.terminal.client import TerminalClient, TerminalServiceError

_TICKET_TTL_S = 60.0
_READ_CHUNK = 65536
_WATCHDOG_TICK_S = 1.0
# How often the live bridge re-checks that the authorizing session is still valid.
_REVALIDATE_S = 15.0

log = get_logger(__name__)


@dataclass
class TerminalTicket:
    session_id: str
    ticket_hash: str
    created_at: datetime
    expires_at: datetime
    used: bool = False


@dataclass
class _BridgeState:
    """Mutable deadlines shared between the bridge pumps and the watchdog."""

    idle_timeout_s: float
    deadline: float
    idle_at: float

    def bump_idle(self) -> None:
        self.idle_at = asyncio.get_running_loop().time() + self.idle_timeout_s


@dataclass
class TerminalManager:
    """Tracks the one permitted terminal session, its ticket, and the live PTY."""

    settings: Settings
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _ticket: TerminalTicket | None = None
    _active: bool = False
    _pid: int | None = None
    _client: TerminalClient | None = None

    # -- ticket lifecycle ---------------------------------------------------

    def issue_ticket(self) -> tuple[str, str]:
        """Create a one-time ticket; returns (ticket_plaintext, session_id)."""
        ticket = secrets.token_urlsafe(32)
        session_id = secrets.token_urlsafe(16)
        now = datetime.now(tz=UTC)
        self._ticket = TerminalTicket(
            session_id=session_id,
            ticket_hash=hashlib.sha256(ticket.encode()).hexdigest(),
            created_at=now,
            expires_at=now + timedelta(seconds=_TICKET_TTL_S),
        )
        return ticket, session_id

    def consume_ticket(self, ticket: str) -> str | None:
        """Validate and consume a ticket; returns session_id or None if invalid."""
        current = self._ticket
        if current is None or current.used:
            return None
        if datetime.now(tz=UTC) > current.expires_at:
            return None
        presented = hashlib.sha256(ticket.encode()).hexdigest()
        if not secrets.compare_digest(presented, current.ticket_hash):
            return None
        current.used = True
        return current.session_id

    def has_active(self) -> bool:
        return self._active

    async def aclose(self) -> None:
        """Release resources on shutdown: drop the ticket and kill any live PTY."""
        self._ticket = None
        await self.close_pty()

    # -- terminal service connection ---------------------------------------

    def _terminal_client(self) -> TerminalClient:
        if self._client is None:
            self._client = TerminalClient(self.settings.paths.terminal_socket)
        return self._client

    async def probe(self) -> None:
        """Verify the terminal service is reachable; raises TerminalUnavailableError."""
        await self._terminal_client().aping()

    async def open_pty(self, cols: int, rows: int) -> tuple[int, int]:
        """Open the single permitted PTY; returns (pid, master_fd owned by caller).

        Raises:
            ConflictError: A terminal session is already active.
            TerminalUnavailableError / TerminalServiceError: Service failure.
        """
        async with self._lock:
            if self._active:
                raise ConflictError("A terminal session is already active.")
            pid, master_fd = await self._terminal_client().aopen(cols, rows)
            self._active = True
            self._pid = pid
            return pid, master_fd

    async def close_pty(self) -> int | None:
        """Close the active PTY (if any); returns the shell's exit status."""
        async with self._lock:
            pid = self._pid
            self._pid = None
            self._active = False
        if pid is None:
            return None
        try:
            return await self._terminal_client().aclose(pid)
        except (TerminalUnavailableError, TerminalServiceError) as exc:
            log.warning("terminal.close_failed", pid=pid, error=str(exc))
            return None

    async def resize(self, cols: int, rows: int) -> None:
        pid = self._pid
        if pid is None:
            return
        with contextlib.suppress(TerminalUnavailableError, TerminalServiceError):
            await self._terminal_client().aresize(pid, cols, rows)

    # -- WebSocket <-> PTY bridge -------------------------------------------

    async def bridge(
        self,
        websocket: WebSocket,
        master_fd: int,
        *,
        idle_timeout_s: float,
        max_duration_s: float,
        revalidate: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """Bridge the PTY master fd and the WebSocket until either side ends.

        Returns when the shell exits, the socket disconnects, an idle/max
        deadline is hit, or ``revalidate`` reports the authorizing session is no
        longer valid (revoked/logged-out/password-changed). Does NOT close
        ``master_fd`` (the caller owns it).
        """
        loop = asyncio.get_running_loop()
        os.set_blocking(master_fd, False)
        to_ws: asyncio.Queue[bytes | None] = asyncio.Queue()
        now = loop.time()
        state = _BridgeState(
            idle_timeout_s=idle_timeout_s,
            deadline=now + max_duration_s,
            idle_at=now + idle_timeout_s,
        )

        def _on_readable() -> None:
            try:
                data = os.read(master_fd, _READ_CHUNK)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            to_ws.put_nowait(data if data else None)

        loop.add_reader(master_fd, _on_readable)
        tasks = [
            asyncio.create_task(self._pty_to_ws(websocket, to_ws, state)),
            asyncio.create_task(self._ws_to_pty(websocket, master_fd, state)),
            asyncio.create_task(self._watchdog(state)),
        ]
        if revalidate is not None:
            tasks.append(asyncio.create_task(self._session_guard(revalidate)))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            loop.remove_reader(master_fd)
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _pty_to_ws(
        self, websocket: WebSocket, to_ws: asyncio.Queue[bytes | None], state: _BridgeState
    ) -> None:
        while True:
            data = await to_ws.get()
            if data is None:
                return  # EOF: the shell exited
            await websocket.send_bytes(data)
            state.bump_idle()

    async def _ws_to_pty(self, websocket: WebSocket, master_fd: int, state: _BridgeState) -> None:
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return
            if message["type"] == "websocket.disconnect":
                return
            state.bump_idle()
            data = message.get("bytes")
            if data is not None:
                await self._write_all(master_fd, data)
                continue
            text = message.get("text")
            if text is not None:
                await self._handle_control(text)

    async def _handle_control(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if message.get("type") == "resize":
            cols = message.get("cols")
            rows = message.get("rows")
            if isinstance(cols, int) and isinstance(rows, int):
                await self.resize(cols, rows)
        # {"type": "ping"} is a client keepalive; no server action required.

    @staticmethod
    async def _write_all(fd: int, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                await _wait_writable(loop, fd)
                continue
            except OSError:
                return
            view = view[written:]

    @staticmethod
    async def _watchdog(state: _BridgeState) -> None:
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            if now >= state.deadline or now >= state.idle_at:
                return
            await asyncio.sleep(min(_WATCHDOG_TICK_S, state.idle_at - now))

    @staticmethod
    async def _session_guard(revalidate: Callable[[], Awaitable[bool]]) -> None:
        """Periodically re-check the authorizing session; return once it is gone.

        Closing the terminal the moment the Owner revokes the session / logs out
        / changes the password is the only remediation available for a stolen
        cookie, so the live root PTY must not outlive its session.
        """
        while True:
            await asyncio.sleep(_REVALIDATE_S)
            try:
                still_valid = await revalidate()
            except Exception:
                log.warning("terminal.revalidate_failed")
                return
            if not still_valid:
                log.info("terminal.session_revoked_midstream")
                return


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
