"""PTY server for the ``ownpanel-terminal`` root service (code-plan-m2-m5 §5.1).

Serves a framed Unix-socket protocol (reusing :mod:`ownpanel.ipc.framing` and the
:class:`~ownpanel.ipc.messages.HelperRequest`/``HelperResponse`` envelope) with an
``SO_PEERCRED`` UID check identical to the general helper. Unlike the helper this
service owns exactly one live PTY at a time: ``terminal.open`` forks ``/bin/bash``
and hands the PTY master fd back to the API over ``SCM_RIGHTS`` while retaining its
own copy for ``terminal.resize``/``terminal.close``.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import socket
import struct
import termios
import threading
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ownpanel.ipc.framing import FrameEOFError, FramingError, recv_frame, send_frame
from ownpanel.ipc.messages import HelperError, HelperRequest, HelperResponse
from ownpanel.logging import get_logger

_PEERCRED = struct.Struct("3i")  # (pid, uid, gid) as returned by SO_PEERCRED
_ACCEPT_POLL_S = 0.5

# Terminal wire error codes (service-local; surfaced to the API-side client).
TERMINAL_BUSY = "terminal.busy"
TERMINAL_NO_SESSION = "terminal.no_session"
TERMINAL_UNKNOWN_ACTION = "terminal.unknown_action"
TERMINAL_INVALID_ARGS = "terminal.invalid_args"
TERMINAL_INTERNAL = "terminal.internal"
TERMINAL_PROTOCOL = "terminal.protocol"

_SHELL = "/bin/bash"
_MAX_DIM = 1000
_REAP_SIGHUP_S = 3.0
_REAP_SIGKILL_S = 2.0

log = get_logger(__name__)


class _ServiceError(Exception):
    """Business-level failure carrying a stable terminal.* wire code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OpenArgs(BaseModel):
    cols: int = Field(default=80, gt=0, le=_MAX_DIM)
    rows: int = Field(default=24, gt=0, le=_MAX_DIM)


class CloseArgs(BaseModel):
    pid: int


class ResizeArgs(BaseModel):
    pid: int
    cols: int = Field(gt=0, le=_MAX_DIM)
    rows: int = Field(gt=0, le=_MAX_DIM)


class PingArgs(BaseModel):
    pass


@dataclass
class _PtySession:
    pid: int
    master_fd: int


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


class TerminalService:
    """Holds the single active PTY and executes the ``terminal.*`` actions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: _PtySession | None = None

    def open(self, args: OpenArgs) -> tuple[dict[str, Any], int]:
        """Fork a login shell on a fresh PTY; return ({pid}, master_fd to attach).

        The service keeps its own copy of the master fd (for resize/close); the
        attached fd is a ``SCM_RIGHTS`` duplicate handed to the API.
        """
        with self._lock:
            if self._session is not None:
                # Reclaim a stale/orphaned session. The API enforces one live
                # terminal, so a fresh open means the previous opener (API
                # process) is gone (e.g. it crashed without sending close);
                # newest-wins keeps the terminal usable instead of wedging on
                # TERMINAL_BUSY forever.
                log.warning("terminal.reclaiming_session", old_pid=self._session.pid)
                self._terminate_locked(self._session)
                self._session = None
            master, slave = os.openpty()
            try:
                _set_winsize(master, args.cols, args.rows)
                pid = os.fork()
            except Exception:
                os.close(master)
                os.close(slave)
                raise
            if pid == 0:
                # Child: attach the slave as the controlling terminal, then exec.
                try:
                    os.close(master)
                    os.login_tty(slave)
                    os.environ["TERM"] = "xterm-256color"
                    os.execv(_SHELL, [_SHELL, "-l"])
                except Exception:
                    os._exit(127)
                os._exit(127)  # pragma: no cover - execv replaced the process
            os.close(slave)
            self._session = _PtySession(pid=pid, master_fd=master)
            log.info("terminal.opened", pid=pid, cols=args.cols, rows=args.rows)
            return {"pid": pid}, master

    def close(self, args: CloseArgs) -> dict[str, Any]:
        with self._lock:
            session = self._session
            if session is None or session.pid != args.pid:
                raise _ServiceError(TERMINAL_NO_SESSION, "no such terminal session")
            exit_status = self._terminate_locked(session)
            self._session = None
            log.info("terminal.closed", pid=session.pid, exit_status=exit_status)
            return {"exit_status": exit_status}

    def resize(self, args: ResizeArgs) -> dict[str, Any]:
        with self._lock:
            session = self._session
            if session is None or session.pid != args.pid:
                raise _ServiceError(TERMINAL_NO_SESSION, "no such terminal session")
            try:
                _set_winsize(session.master_fd, args.cols, args.rows)
            except OSError as exc:
                raise _ServiceError(TERMINAL_INTERNAL, f"resize failed: {exc}") from exc
        return {"ok": True}

    def shutdown_active(self) -> None:
        """Tear down any live PTY on service shutdown."""
        with self._lock:
            session = self._session
            if session is not None:
                self._terminate_locked(session)
                self._session = None

    def _terminate_locked(self, session: _PtySession) -> int | None:
        with contextlib.suppress(OSError):
            os.close(session.master_fd)
        with contextlib.suppress(ProcessLookupError):
            os.kill(session.pid, signal.SIGHUP)
        exit_status = self._reap(session.pid, timeout_s=_REAP_SIGHUP_S)
        if exit_status is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(session.pid, signal.SIGKILL)
            exit_status = self._reap(session.pid, timeout_s=_REAP_SIGKILL_S)
        return exit_status

    @staticmethod
    def _reap(pid: int, *, timeout_s: float) -> int | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if waited == pid:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
        return None


def _validate[ModelT: BaseModel](model: type[ModelT], raw: dict[str, Any]) -> ModelT:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise _ServiceError(TERMINAL_INVALID_ARGS, "invalid args") from exc


def _send_error(conn: socket.socket, request_id: str, code: str, message: str) -> None:
    response = HelperResponse(
        id=request_id, ok=False, error=HelperError(code=code, message=message)
    )
    send_frame(conn, response.model_dump())


class TerminalServer:
    """Accept loop + per-connection dispatch for the terminal service socket."""

    def __init__(self, sock: socket.socket, allowed_uids: frozenset[int]) -> None:
        self._sock = sock
        self._allowed_uids = allowed_uids
        self._service = TerminalService()
        self._stopping = threading.Event()

    def serve_forever(self) -> None:
        """Accept connections until :meth:`shutdown`; stop noticed within _ACCEPT_POLL_S."""
        self._sock.settimeout(_ACCEPT_POLL_S)
        while not self._stopping.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                raise
            threading.Thread(
                target=self._serve_connection,
                args=(conn,),
                name=f"terminal-conn-{conn.fileno()}",
                daemon=True,
            ).start()

    def shutdown(self) -> None:
        """Stop accepting (idempotent) and tear down any live PTY."""
        self._stopping.set()
        self._sock.close()
        self._service.shutdown_active()

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn:
            if not self._peer_allowed(conn):
                return
            try:
                self._serve_requests(conn)
            except Exception:
                log.exception("terminal.connection_crashed")

    def _peer_allowed(self, conn: socket.socket) -> bool:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
        pid, uid, gid = _PEERCRED.unpack(creds)
        if uid not in self._allowed_uids:
            log.warning("terminal.peer_denied", pid=pid, uid=uid, gid=gid)
            return False
        return True

    def _serve_requests(self, conn: socket.socket) -> None:
        while True:
            try:
                payload, _fds = recv_frame(conn, max_fds=0)
            except FrameEOFError:
                return
            except (FramingError, OSError) as exc:
                log.warning("terminal.connection_aborted", error=str(exc))
                return
            try:
                self._handle_request(conn, payload)
            except OSError as exc:
                log.warning("terminal.connection_aborted", error=str(exc))
                return

    def _handle_request(self, conn: socket.socket, payload: dict[str, Any]) -> None:
        raw_id = payload.get("id")
        request_id = raw_id if isinstance(raw_id, str) else ""
        try:
            request = HelperRequest.model_validate(payload)
        except ValidationError:
            _send_error(conn, request_id, TERMINAL_PROTOCOL, "malformed request frame")
            return
        if request.v != 1:
            _send_error(
                conn, request.id, TERMINAL_PROTOCOL, f"unsupported protocol version {request.v}"
            )
            return
        try:
            result, fds = self._dispatch(request.action, request.args)
        except _ServiceError as exc:
            _send_error(conn, request.id, exc.code, exc.message)
            return
        except Exception:
            log.exception("terminal.action_crashed", action=request.action)
            _send_error(conn, request.id, TERMINAL_INTERNAL, "internal terminal error")
            return
        self._send_response(conn, request.id, result, fds)

    def _send_response(
        self, conn: socket.socket, request_id: str, result: dict[str, Any], fds: list[int]
    ) -> None:
        response = HelperResponse(id=request_id, ok=True, result=result, fds=len(fds))
        sent = False
        try:
            # NOTE: unlike the helper we do NOT close ``fds`` afterwards — the master
            # fd is retained by the service for the session's lifetime.
            send_frame(conn, response.model_dump(), fds)
            sent = True
        except (FramingError, UnicodeError) as exc:
            log.error("terminal.response_unserializable", request_id=request_id, error=str(exc))
            with contextlib.suppress(OSError, FramingError):
                _send_error(conn, request_id, TERMINAL_INTERNAL, "response could not be serialized")
        except OSError as exc:
            log.warning("terminal.response_send_failed", request_id=request_id, error=str(exc))
        finally:
            if not sent and fds:
                # A PTY master we could not deliver would orphan a shell; tear it down.
                pid = result.get("pid")
                if isinstance(pid, int):
                    with contextlib.suppress(Exception):
                        self._service.close(CloseArgs(pid=pid))

    def _dispatch(self, action: str, raw_args: dict[str, Any]) -> tuple[dict[str, Any], list[int]]:
        if action == "terminal.ping":
            _validate(PingArgs, raw_args)
            return {"pong": True}, []
        if action == "terminal.open":
            result, master = self._service.open(_validate(OpenArgs, raw_args))
            return result, [master]
        if action == "terminal.close":
            return self._service.close(_validate(CloseArgs, raw_args)), []
        if action == "terminal.resize":
            return self._service.resize(_validate(ResizeArgs, raw_args)), []
        raise _ServiceError(TERMINAL_UNKNOWN_ACTION, f"unknown action: {action}")
