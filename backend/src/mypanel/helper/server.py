"""Helper connection server: accept loop, SO_PEERCRED UID check, frame dispatch.

Threading model: the caller's thread runs accept(); every accepted connection
is served on its own daemon thread. The single expected client is the API
process, so there is no connection or request concurrency to manage.
"""

from __future__ import annotations

import socket
import struct
import threading
from typing import Any

from pydantic import ValidationError

from mypanel.helper.registry import ActionError, dispatch
from mypanel.ipc.fdpass import close_fds
from mypanel.ipc.framing import FrameEOFError, FramingError, recv_frame, send_frame
from mypanel.ipc.messages import (
    HELPER_INTERNAL,
    HELPER_PROTOCOL,
    HelperError,
    HelperRequest,
    HelperResponse,
)
from mypanel.logging import get_logger

_PEERCRED = struct.Struct("3i")  # (pid, uid, gid) as returned by SO_PEERCRED

# accept() polling interval: closing a socket in one thread does not wake an
# accept() blocked on it in another, so the loop must recheck the stop flag.
_ACCEPT_POLL_S = 0.5

log = get_logger(__name__)


def _send_error(conn: socket.socket, request_id: str, code: str, message: str) -> None:
    response = HelperResponse(
        id=request_id, ok=False, error=HelperError(code=code, message=message)
    )
    send_frame(conn, response.model_dump())


class HelperServer:
    """Serves helper requests on an already-listening Unix stream socket."""

    def __init__(self, sock: socket.socket, allowed_uids: frozenset[int]) -> None:
        self._sock = sock
        self._allowed_uids = allowed_uids
        self._stopping = threading.Event()

    def serve_forever(self) -> None:
        """Accept connections until shutdown(); stop is noticed within _ACCEPT_POLL_S."""
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
                name=f"helper-conn-{conn.fileno()}",
                daemon=True,
            ).start()

    def shutdown(self) -> None:
        """Stop accepting (idempotent): set the stop flag and close the listening socket."""
        self._stopping.set()
        self._sock.close()

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn:
            if not self._peer_allowed(conn):
                return
            try:
                self._serve_requests(conn)
            except Exception:
                # A connection thread must never die with a raw traceback (which would
                # bypass structured logging and, mid-response, leave the client waiting
                # on a socket that only EOFs). Log and let ``with conn`` close cleanly.
                log.exception("helper.connection_crashed")

    def _serve_requests(self, conn: socket.socket) -> None:
        while True:
            try:
                # M1 requests never carry descriptors; any that arrive are a
                # protocol violation (FramingError) and end the connection.
                payload, _fds = recv_frame(conn, max_fds=0)
            except FrameEOFError:
                return
            except (FramingError, OSError) as exc:
                log.warning("helper.connection_aborted", error=str(exc))
                return
            try:
                self._handle_request(conn, payload)
            except OSError as exc:
                log.warning("helper.connection_aborted", error=str(exc))
                return

    def _peer_allowed(self, conn: socket.socket) -> bool:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
        pid, uid, gid = _PEERCRED.unpack(creds)
        if uid not in self._allowed_uids:
            log.warning("helper.peer_denied", pid=pid, uid=uid, gid=gid)
            return False
        return True

    def _handle_request(self, conn: socket.socket, payload: dict[str, Any]) -> None:
        """Dispatch one request frame and send exactly one response frame."""
        raw_id = payload.get("id")
        request_id = raw_id if isinstance(raw_id, str) else ""
        try:
            request = HelperRequest.model_validate(payload)
        except ValidationError:
            _send_error(conn, request_id, HELPER_PROTOCOL, "malformed request frame")
            return
        if request.v != 1:
            _send_error(
                conn, request.id, HELPER_PROTOCOL, f"unsupported protocol version {request.v}"
            )
            return
        try:
            outcome = dispatch(request.action, request.args)
        except ActionError as exc:
            _send_error(conn, request.id, exc.code, exc.message)
            return
        except Exception:
            log.exception("helper.action_crashed", action=request.action, request_id=request.id)
            _send_error(conn, request.id, HELPER_INTERNAL, "internal helper error")
            return
        response = HelperResponse(
            id=request.id, ok=True, result=outcome.result, fds=len(outcome.fds)
        )
        try:
            send_frame(conn, response.model_dump(), outcome.fds)
        except (FramingError, UnicodeError) as exc:
            # Oversized (FramingError) or unserialisable (UnicodeError, e.g. a stray
            # lone surrogate from a filesystem string) response: report a clean helper
            # error instead of letting it crash the connection thread.
            log.error(
                "helper.response_unserializable",
                action=request.action,
                request_id=request.id,
                error=str(exc),
            )
            _send_error(conn, request.id, HELPER_INTERNAL, "response could not be serialized")
        finally:
            # SCM_RIGHTS duplicated the descriptors into the peer; drop our copies.
            close_fds(outcome.fds)
