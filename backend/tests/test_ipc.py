"""Wire-protocol tests for mypanel.ipc: framing, fd passing, message models.

docs/code-plan.md section 4: a frame is a 4-byte big-endian unsigned length
followed by a UTF-8 JSON object of at most 256 KiB. Descriptors travel as one
SCM_RIGHTS message on the sendmsg call carrying the first frame byte. All
tests here run over an AF_UNIX socketpair; no helper process is involved.
"""

from __future__ import annotations

import json
import os
import socket
import struct
from collections.abc import Iterator

import pytest

from mypanel.ipc.framing import (
    MAX_FRAME_BYTES,
    FrameEOFError,
    FramingError,
    recv_frame,
    send_frame,
)
from mypanel.ipc.messages import (
    HELPER_DENIED,
    HELPER_INTERNAL,
    HELPER_INVALID_ARGS,
    HELPER_NOT_A_DIRECTORY,
    HELPER_PATH_INVALID,
    HELPER_PATH_NOT_FOUND,
    HELPER_PROTOCOL,
    HELPER_TIMEOUT,
    HELPER_UNKNOWN_ACTION,
    HelperError,
    HelperRequest,
    HelperResponse,
)

SocketPair = tuple[socket.socket, socket.socket]


def _open_fd_count() -> int:
    """Number of open descriptors in this process (Linux)."""
    return len(os.listdir("/proc/self/fd"))


def _read_exactly(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        assert chunk, f"peer closed after {len(buf)}/{size} bytes"
        buf += chunk
    return buf


@pytest.fixture
def pair() -> Iterator[SocketPair]:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    a.settimeout(5)
    b.settimeout(5)
    yield a, b
    a.close()
    b.close()


# ---------------------------------------------------------------------------
# Frame round-trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 1, "id": "req-1", "args": {"path": "/etc"}, "list": [1, 2.5, None, True]},
        {"text": "héllo wörld 你好 世界 🚀", "path": "/tmp/naïve.txt"},
        {},
    ],
    ids=["nested", "unicode", "empty-dict"],
)
def test_frame_roundtrip(pair: SocketPair, payload: dict[str, object]) -> None:
    a, b = pair
    send_frame(a, payload)
    received, fds = recv_frame(b)
    assert received == payload
    assert fds == []


def test_wire_format_is_big_endian_length_plus_utf8_json(pair: SocketPair) -> None:
    """Pin the on-wire layout itself, independent of recv_frame."""
    a, b = pair
    payload = {"greeting": "héllo 你好", "n": 7}
    send_frame(a, payload)
    header = _read_exactly(b, 4)
    (length,) = struct.unpack(">I", header)
    body = _read_exactly(b, length)
    assert json.loads(body.decode("utf-8")) == payload
    a.close()
    assert b.recv(1) == b""  # nothing follows the declared body


# ---------------------------------------------------------------------------
# File-descriptor passing
# ---------------------------------------------------------------------------


def test_fd_passing_roundtrip_and_full_cleanup(pair: SocketPair) -> None:
    """Send a pipe read end with the frame, keep the write end, talk through it."""
    a, b = pair
    baseline = _open_fd_count()
    r, w = os.pipe()
    send_frame(a, {"kind": "fd-carrier"}, [r])
    payload, fds = recv_frame(b)
    try:
        assert payload == {"kind": "fd-carrier"}
        assert len(fds) == 1
        dup = fds[0]
        # SCM_RIGHTS delivers a duplicate descriptor, not the sender's number.
        assert dup != r
        os.write(w, b"through-the-pipe")
        assert os.read(dup, 64) == b"through-the-pipe"
    finally:
        for fd in (r, w, *fds):
            os.close(fd)
    # Closing the write end plus every read end leaves the fd table unchanged.
    assert _open_fd_count() == baseline


def test_fd_passing_count_matches_on_both_sides(pair: SocketPair) -> None:
    a, b = pair
    pipes = [os.pipe() for _ in range(3)]
    read_ends = [r for r, _ in pipes]
    send_frame(a, {"n": len(read_ends)}, read_ends)
    payload, fds = recv_frame(b)
    try:
        assert payload == {"n": 3}
        assert len(fds) == len(read_ends) == 3
        # Order is preserved: each duplicate maps to the pipe sent at that slot.
        for index, ((_r, w), dup) in enumerate(zip(pipes, fds, strict=True)):
            marker = f"pipe-{index}".encode()
            os.write(w, marker)
            assert os.read(dup, 64) == marker
    finally:
        for r, w in pipes:
            os.close(r)
            os.close(w)
        for dup in fds:
            os.close(dup)


def test_recv_error_path_closes_received_fds(pair: SocketPair) -> None:
    """A frame that fails JSON decoding must not leak the descriptors it carried."""
    a, b = pair
    r, w = os.pipe()
    body = b"{ this is not json"
    socket.send_fds(a, [struct.pack(">I", len(body)) + body], [r])
    os.close(r)
    os.close(w)
    baseline = _open_fd_count()
    with pytest.raises(FramingError, match="not valid UTF-8 JSON"):
        recv_frame(b)
    assert _open_fd_count() == baseline


# ---------------------------------------------------------------------------
# Size limits
# ---------------------------------------------------------------------------


def test_send_oversized_payload_raises_without_writing(pair: SocketPair) -> None:
    a, b = pair
    payload = {"data": "x" * MAX_FRAME_BYTES}  # serialised body > MAX_FRAME_BYTES
    with pytest.raises(FramingError, match="exceeds") as excinfo:
        send_frame(a, payload)
    assert not isinstance(excinfo.value, FrameEOFError)
    a.close()
    assert b.recv(1) == b""  # not a single byte reached the peer


def test_recv_rejects_oversized_length_header(pair: SocketPair) -> None:
    a, b = pair
    a.sendall(struct.pack(">I", 300 * 1024) + b"garbage-that-never-gets-parsed")
    with pytest.raises(FramingError, match="exceeds"):
        recv_frame(b)


# ---------------------------------------------------------------------------
# EOF handling
# ---------------------------------------------------------------------------


def test_frame_eof_error_is_a_framing_error() -> None:
    assert issubclass(FrameEOFError, FramingError)


def test_recv_on_closed_peer_raises_frame_eof(pair: SocketPair) -> None:
    a, b = pair
    a.close()
    with pytest.raises(FrameEOFError):
        recv_frame(b)


def test_recv_partial_length_header_then_close(pair: SocketPair) -> None:
    a, b = pair
    a.sendall(b"\x00\x00")  # 2 of the 4 header bytes
    a.close()
    with pytest.raises(FrameEOFError):
        recv_frame(b)


def test_recv_partial_body_then_close(pair: SocketPair) -> None:
    a, b = pair
    a.sendall(struct.pack(">I", 10) + b"abc")  # header claims 10, only 3 arrive
    a.close()
    with pytest.raises(FrameEOFError):
        recv_frame(b)


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [b"[1,2,3]", b'"text"', b"null", b"42"],
    ids=["array", "string", "null", "number"],
)
def test_recv_rejects_non_object_top_level(pair: SocketPair, body: bytes) -> None:
    a, b = pair
    a.sendall(struct.pack(">I", len(body)) + body)
    with pytest.raises(FramingError, match="JSON object"):
        recv_frame(b)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


def test_helper_request_defaults_and_roundtrip() -> None:
    request = HelperRequest(id="req-1", action="ping")
    assert request.v == 1
    assert request.args == {}
    assert request.job_id is None
    assert request.timeout_s == 30.0
    dumped = request.model_dump()
    assert dumped == {
        "v": 1,
        "id": "req-1",
        "action": "ping",
        "args": {},
        "job_id": None,
        "timeout_s": 30.0,
    }
    assert HelperRequest.model_validate(dumped) == request


def test_helper_response_defaults_and_roundtrip() -> None:
    response = HelperResponse(id="req-1", ok=True)
    assert response.v == 1
    assert response.result is None
    assert response.error is None
    assert response.fds == 0
    assert HelperResponse.model_validate(response.model_dump()) == response


def test_helper_error_response_roundtrip() -> None:
    response = HelperResponse(
        id="req-2", ok=False, error=HelperError(code="helper.internal", message="boom")
    )
    dumped = response.model_dump()
    assert dumped["error"] == {"code": "helper.internal", "message": "boom"}
    restored = HelperResponse.model_validate(dumped)
    assert restored == response
    assert restored.error is not None
    assert restored.error.code == "helper.internal"


def test_helper_error_codes_are_stable() -> None:
    """Helper error codes are released contract; renaming any of them is a break."""
    assert HELPER_UNKNOWN_ACTION == "helper.unknown_action"
    assert HELPER_INVALID_ARGS == "helper.invalid_args"
    assert HELPER_INTERNAL == "helper.internal"
    assert HELPER_PATH_INVALID == "helper.path_invalid"
    assert HELPER_PATH_NOT_FOUND == "helper.path_not_found"
    assert HELPER_NOT_A_DIRECTORY == "helper.not_a_directory"
    assert HELPER_DENIED == "helper.denied"
    assert HELPER_TIMEOUT == "helper.timeout"
    assert HELPER_PROTOCOL == "helper.protocol"
