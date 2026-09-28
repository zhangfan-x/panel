"""SCM_RIGHTS file-descriptor passing over Unix stream sockets.

Follows the recipe in the Python `socket` documentation: descriptors travel
as a single ancillary control message alongside regular stream data.
"""

from __future__ import annotations

import array
import contextlib
import os
import socket
from collections.abc import Iterable, Sequence

_INT_SIZE = array.array("i").itemsize


class ControlTruncatedError(OSError):
    """Ancillary data was truncated (MSG_CTRUNC) or more fds arrived than allowed."""


def close_fds(fds: Iterable[int]) -> None:
    """Best-effort close of received descriptors (error/cleanup paths)."""
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def send_with_fds(sock: socket.socket, data: bytes, fds: Sequence[int]) -> int:
    """Send `data` with all `fds` attached as one SCM_RIGHTS message on one sendmsg.

    The descriptors are delivered together with the first byte of `data`; the
    send may be partial for large payloads. Returns the number of bytes sent.
    """
    if not fds:
        return sock.sendmsg([data])
    ancdata = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))]
    return sock.sendmsg([data], ancdata)


def recv_with_fds(sock: socket.socket, bufsize: int, max_fds: int) -> tuple[bytes, list[int]]:
    """Receive up to `bufsize` bytes plus at most `max_fds` SCM_RIGHTS descriptors.

    Raises ControlTruncatedError after closing any descriptors already received
    when the kernel reports MSG_CTRUNC or more than `max_fds` descriptors arrive
    (CMSG_SPACE alignment slack can admit an extra one).
    """
    fd_array = array.array("i")
    ancbufsize = socket.CMSG_SPACE(max_fds * _INT_SIZE) if max_fds > 0 else 0
    data, ancdata, flags, _address = sock.recvmsg(bufsize, ancbufsize)
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            # Drop a trailing fragment shorter than one int if the array was cut.
            fd_array.frombytes(cmsg_data[: len(cmsg_data) - (len(cmsg_data) % _INT_SIZE)])
    fds = list(fd_array)
    if flags & socket.MSG_CTRUNC:
        close_fds(fds)
        raise ControlTruncatedError("ancillary data truncated (MSG_CTRUNC)")
    if len(fds) > max_fds:
        close_fds(fds)
        raise ControlTruncatedError(f"received {len(fds)} fds but at most {max_fds} allowed")
    return data, fds
