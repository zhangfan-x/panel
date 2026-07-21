"""Entry point for `mypanel-helper`: socket activation or a self-managed socket."""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import socket
import stat
from pathlib import Path
from types import FrameType

import mypanel

# Importing the actions package registers every handler with the registry.
from mypanel.helper import actions  # noqa: F401
from mypanel.helper.registry import registered_actions
from mypanel.helper.server import HelperServer
from mypanel.logging import get_logger, setup_logging

_LISTEN_FD = 3  # SD_LISTEN_FDS_START

log = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mypanel-helper",
        description="mypanel root helper: privileged actions over a Unix socket",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        metavar="PATH",
        help="listening Unix socket path (ignored under systemd socket activation)",
    )
    parser.add_argument(
        "--allow-uid",
        type=int,
        action="append",
        default=None,
        metavar="UID",
        help="peer UID allowed to connect (repeatable; default: current effective UID)",
    )
    parser.add_argument(
        "--dev", action="store_true", help="pretty console logs instead of JSON"
    )
    return parser


def _socket_from_activation() -> socket.socket | None:
    """Return the systemd-activated listen socket (fd 3) when it was passed to us."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    listen_fds = os.environ.get("LISTEN_FDS")
    if listen_fds is None:
        return None
    if listen_fds != "1":
        raise SystemExit(f"systemd socket activation passed LISTEN_FDS={listen_fds}, expected 1")
    return socket.socket(fileno=_LISTEN_FD)


def _bind_socket(path: Path) -> socket.socket:
    """Bind a fresh 0660 listening socket at `path`, replacing only a stale socket file."""
    try:
        existing_mode: int | None = os.lstat(path).st_mode
    except FileNotFoundError:
        existing_mode = None
    if existing_mode is not None:
        if not stat.S_ISSOCK(existing_mode):
            raise SystemExit(f"refusing to replace non-socket file at {path}")
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # The umask closes the window between bind() creating the file and chmod().
    old_umask = os.umask(0o117)
    try:
        sock.bind(str(path))
        os.chmod(path, 0o660)
        sock.listen(8)
    except OSError as exc:
        sock.close()
        raise SystemExit(f"cannot bind helper socket at {path}: {exc}") from exc
    finally:
        os.umask(old_umask)
    return sock


def main() -> None:
    """Console-script entry point (pyproject: mypanel-helper)."""
    parser = _build_parser()
    args = parser.parse_args()
    setup_logging(dev=args.dev)

    allow_uid: list[int] | None = args.allow_uid
    allowed_uids = frozenset(allow_uid) if allow_uid else frozenset({os.geteuid()})

    sock = _socket_from_activation()
    owned_path: Path | None = None
    if sock is None:
        if args.socket is None:
            parser.error("--socket is required when not started via systemd socket activation")
        sock = _bind_socket(args.socket)
        owned_path = args.socket

    server = HelperServer(sock, allowed_uids=allowed_uids)

    def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
        log.info("helper.signal_received", signal=signal.Signals(signum).name)
        server.shutdown()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    log.info(
        "helper.started",
        version=mypanel.__version__,
        socket=str(owned_path) if owned_path is not None else "systemd-activated",
        allowed_uids=sorted(allowed_uids),
        actions=list(registered_actions()),
    )
    try:
        server.serve_forever()
    finally:
        server.shutdown()
        if owned_path is not None:
            with contextlib.suppress(OSError):
                owned_path.unlink()
    log.info("helper.stopped")
