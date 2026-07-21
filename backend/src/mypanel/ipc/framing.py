"""Length-prefixed JSON framing with optional SCM_RIGHTS descriptor passing.

Wire format: 4-byte big-endian unsigned body length, then a UTF-8 JSON object
of at most MAX_FRAME_BYTES. Descriptors ride on the sendmsg call that carries
the first byte of the frame, so the receiver supplies ancillary buffer space
on its first recvmsg. Peer EOF raises FrameEOFError (a FramingError subclass);
plain EOFError is never used.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Sequence
from typing import Any

from mypanel.ipc.fdpass import ControlTruncatedError, close_fds, recv_with_fds, send_with_fds

MAX_FRAME_BYTES = 256 * 1024
_HEADER_SIZE = 4


class FramingError(Exception):
    """Frame violates the wire protocol (size, encoding, shape, or truncation)."""


class FrameEOFError(FramingError):
    """Peer closed the connection before a complete frame arrived."""


def send_frame(sock: socket.socket, payload: dict[str, Any], fds: Sequence[int] = ()) -> None:
    """Serialise `payload` and send it as one frame, `fds` attached to the first chunk.

    Raises FramingError when the serialised body exceeds MAX_FRAME_BYTES. The
    whole frame goes through a single sendmsg; on a partial send the remaining
    bytes follow via sendall (the descriptors were delivered with the first chunk).
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FramingError(f"frame body of {len(body)} bytes exceeds {MAX_FRAME_BYTES}")
    frame = len(body).to_bytes(_HEADER_SIZE, "big") + body
    sent = send_with_fds(sock, frame, fds)
    if sent < len(frame):
        sock.sendall(frame[sent:])


def _recv_exact(sock: socket.socket, size: int, initial: bytes = b"") -> bytes:
    buf = bytearray(initial)
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise FrameEOFError("peer closed connection mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket, max_fds: int = 8) -> tuple[dict[str, Any], list[int]]:
    """Receive one frame and up to `max_fds` descriptors attached to its first byte.

    Raises FramingError on zero-length or oversized frames, invalid UTF-8/JSON,
    non-object payloads, and ancillary truncation; FrameEOFError when the peer
    closes before or during a frame. Every error path closes descriptors that
    were already received, so callers never inherit leaked fds on failure.
    """
    fds: list[int] = []
    try:
        try:
            first, fds = recv_with_fds(sock, _HEADER_SIZE, max_fds)
        except ControlTruncatedError as exc:
            raise FramingError(str(exc)) from exc
        if not first:
            raise FrameEOFError("peer closed connection")
        header = _recv_exact(sock, _HEADER_SIZE, first)
        length = int.from_bytes(header, "big")
        if length == 0:
            raise FramingError("zero-length frame")
        if length > MAX_FRAME_BYTES:
            raise FramingError(f"frame body of {length} bytes exceeds {MAX_FRAME_BYTES}")
        body = _recv_exact(sock, length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise FramingError(f"frame body is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise FramingError("frame payload must be a JSON object")
        return payload, fds
    except BaseException:
        close_fds(fds)
        raise
