"""End-to-end helper tests: a real HelperServer thread plus HelperClient.

docs/code-plan.md section 4: the server accepts on a Unix socket, checks the
peer UID via SO_PEERCRED, dispatches registered actions and answers each
request with exactly one response frame. The client opens one fresh
connection per call, so protocol state can never stick between calls.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import socket
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import mypanel
from mypanel.errors import HelperActionError, HelperUnavailableError
from mypanel.helper.server import HelperServer
from mypanel.ipc.client import HelperClient

# Importing the actions package registers ping/files.stat/files.list handlers.
importlib.import_module("mypanel.helper.actions")


@contextlib.contextmanager
def running_helper(socket_path: Path, allowed_uids: set[int]) -> Iterator[HelperClient]:
    """Serve a HelperServer on `socket_path` in a thread; yield a client for it."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    sock.listen(8)
    server = HelperServer(sock, allowed_uids=frozenset(allowed_uids))
    thread = threading.Thread(target=server.serve_forever, name="helper-test", daemon=True)
    thread.start()
    try:
        yield HelperClient(socket_path)
    finally:
        server.shutdown()
        thread.join(2)
        assert not thread.is_alive(), "helper server thread failed to stop"


@pytest.fixture(scope="module")
def helper(tmp_path_factory: pytest.TempPathFactory) -> Iterator[HelperClient]:
    """Client for a module-wide helper server that allows the current UID."""
    socket_dir = tmp_path_factory.mktemp("helper-sock")
    with running_helper(socket_dir / "helper.sock", {os.geteuid()}) as client:
        yield client


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


def test_ping_reports_version(helper: HelperClient) -> None:
    result = helper.call("ping")
    assert result == {"pong": True, "helper_version": mypanel.__version__}
    assert result["pong"] is True


# ---------------------------------------------------------------------------
# files.stat
# ---------------------------------------------------------------------------


def test_stat_regular_file_fields(helper: HelperClient, tmp_path: Path) -> None:
    path = tmp_path / "regular.bin"
    content = b"exactly-seventeen"
    path.write_bytes(content)
    os.chmod(path, 0o640)
    os.utime(path, (1_700_000_000.25, 1_700_000_000.25))
    st = os.lstat(path)

    result = helper.call("files.stat", {"path": str(path)})

    assert result == {
        "type": "file",
        "size": len(content),
        "mode": stat.S_IMODE(st.st_mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mtime": st.st_mtime,
    }
    assert result["mode"] == 0o640
    assert isinstance(result["mtime"], float)
    assert result["mtime"] == 1_700_000_000.25
    assert "target" not in result  # only symlinks carry a target


def test_stat_symlink_uses_lstat_and_reports_target(helper: HelperClient, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("hello")
    link = tmp_path / "link"
    os.symlink(target, link)

    result = helper.call("files.stat", {"path": str(link)})

    assert result["type"] == "symlink"  # lstat: a followed stat would say "file"
    assert result["target"] == str(target)
    assert result["size"] == len(str(target))  # lstat size of a symlink


def test_stat_directory(helper: HelperClient, tmp_path: Path) -> None:
    sub = tmp_path / "subdir"
    sub.mkdir()
    result = helper.call("files.stat", {"path": str(sub)})
    assert result["type"] == "dir"
    assert "target" not in result


# ---------------------------------------------------------------------------
# files.list
# ---------------------------------------------------------------------------


@pytest.fixture
def listing_dir(tmp_path: Path) -> Path:
    root = tmp_path / "listing"
    root.mkdir()
    (root / "b.txt").write_bytes(b"0123456789")
    (root / ".hidden").write_text("secret")
    (root / "adir").mkdir()
    os.symlink(root / "b.txt", root / "clink")
    return root


def test_list_hides_dotfiles_by_default_and_sorts_by_name(
    helper: HelperClient, listing_dir: Path
) -> None:
    result = helper.call("files.list", {"path": str(listing_dir)})
    entries = result["entries"]
    assert [entry["name"] for entry in entries] == ["adir", "b.txt", "clink"]
    by_name = {entry["name"]: entry for entry in entries}
    assert by_name["adir"]["type"] == "dir"
    assert by_name["b.txt"]["type"] == "file"
    assert by_name["b.txt"]["size"] == 10
    assert by_name["clink"]["type"] == "symlink"  # entry stat does not follow links
    for entry in entries:
        # M1 contract: fixed field set, no `target` even for symlink entries.
        assert set(entry) == {"name", "type", "size", "mode", "uid", "gid", "mtime"}


def test_list_include_hidden(helper: HelperClient, listing_dir: Path) -> None:
    result = helper.call("files.list", {"path": str(listing_dir), "include_hidden": True})
    assert [entry["name"] for entry in result["entries"]] == [
        ".hidden",
        "adir",
        "b.txt",
        "clink",
    ]


def test_list_tolerates_non_utf8_filename(helper: HelperClient, tmp_path: Path) -> None:
    """A file whose raw name is not valid UTF-8 must not crash the connection thread
    or lose the whole listing; the name comes back as a displayable, JSON-safe str."""
    root = tmp_path / "badnames"
    root.mkdir()
    (root / "good.txt").write_bytes(b"x")
    raw = os.fsencode(str(root)) + b"/bad\xff\xfe.txt"
    os.close(os.open(raw, os.O_CREAT | os.O_WRONLY, 0o644))

    result = helper.call("files.list", {"path": str(root)})

    names = [entry["name"] for entry in result["entries"]]
    assert "good.txt" in names  # the well-named sibling is not dropped
    assert len(names) == 2
    for name in names:
        assert isinstance(name, str)
        name.encode("utf-8")  # JSON/UTF-8 encodable: no lone surrogate leaked through
    # The server survived: a later call on a fresh connection still works.
    assert helper.call("ping")["pong"] is True


def test_stat_tolerates_non_utf8_symlink_target(helper: HelperClient, tmp_path: Path) -> None:
    """A symlink whose target bytes are not valid UTF-8 stats cleanly rather than
    crashing files.stat with an uncaught UnicodeEncodeError."""
    link = tmp_path / "badlink"
    os.symlink(b"/tmp/\xff\xfe-target", os.fsencode(str(link)))

    result = helper.call("files.stat", {"path": str(link)})

    assert result["type"] == "symlink"
    assert isinstance(result["target"], str)
    result["target"].encode("utf-8")  # JSON/UTF-8 safe
    assert helper.call("ping")["pong"] is True


# ---------------------------------------------------------------------------
# Path validation and filesystem errors
# ---------------------------------------------------------------------------

INVALID_PATHS = [
    pytest.param("relative/x", id="relative"),
    pytest.param("/a/../b", id="dotdot"),
    pytest.param("/a//b", id="double-slash"),
    pytest.param("/a/./b", id="dot-segment"),
    pytest.param("/etc/", id="trailing-slash"),
    pytest.param("//etc", id="double-leading-slash"),
    pytest.param("/a\0b", id="nul-byte"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("path", INVALID_PATHS)
def test_stat_rejects_invalid_paths(helper: HelperClient, path: str) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("files.stat", {"path": path})
    assert excinfo.value.helper_code == "helper.path_invalid"


def test_list_shares_path_validation(helper: HelperClient) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("files.list", {"path": "/x/../y"})
    assert excinfo.value.helper_code == "helper.path_invalid"


def test_stat_missing_path_is_not_found(helper: HelperClient, tmp_path: Path) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("files.stat", {"path": str(tmp_path / "does-not-exist")})
    assert excinfo.value.helper_code == "helper.path_not_found"


def test_list_missing_path_is_not_found(helper: HelperClient, tmp_path: Path) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("files.list", {"path": str(tmp_path / "does-not-exist")})
    assert excinfo.value.helper_code == "helper.path_not_found"


def test_list_on_regular_file_is_not_a_directory(helper: HelperClient, tmp_path: Path) -> None:
    file_path = tmp_path / "plain.txt"
    file_path.write_text("x")
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("files.list", {"path": str(file_path)})
    assert excinfo.value.helper_code == "helper.not_a_directory"


# ---------------------------------------------------------------------------
# Dispatch errors
# ---------------------------------------------------------------------------


def test_unknown_action(helper: HelperClient) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("files.chmod", {"path": "/etc"})
    assert excinfo.value.helper_code == "helper.unknown_action"


def test_stat_without_path_arg_is_invalid_args(helper: HelperClient) -> None:
    with pytest.raises(HelperActionError) as excinfo:
        helper.call("files.stat", {})
    assert excinfo.value.helper_code == "helper.invalid_args"


# ---------------------------------------------------------------------------
# Peer UID enforcement and liveness
# ---------------------------------------------------------------------------


def test_wrong_peer_uid_is_rejected(tmp_path: Path) -> None:
    """A server that only allows a different UID drops us without a response."""
    with (
        running_helper(tmp_path / "denied.sock", {os.geteuid() + 1}) as client,
        pytest.raises(HelperUnavailableError),
    ):
        client.call("ping", timeout_s=5.0)


def test_failed_call_does_not_poison_later_calls(helper: HelperClient) -> None:
    """Every call is a fresh connection, so an error leaves no sticky state."""
    with pytest.raises(HelperActionError):
        helper.call("files.stat", {"path": "relative/x"})
    assert helper.call("ping")["pong"] is True
