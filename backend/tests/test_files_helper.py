"""Files helper actions (docs/code-plan-m2-m5.md §3.3): staging, mutation, safety.

Covers the /proc /sys /dev write guard, symlink refusal on write targets,
Zip-Slip containment, decompression-bomb caps and the etag-guarded atomic
install flow, all against a real in-process HelperServer.
"""

from __future__ import annotations

import importlib
import io
import os
import stat
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_helper import running_helper

from ownpanel.errors import HelperActionError
from ownpanel.helper.actions import files as files_actions
from ownpanel.ipc.client import HelperClient
from ownpanel.ipc.fdpass import close_fds

# Importing the actions package registers every files.* handler.
importlib.import_module("ownpanel.helper.actions")


@pytest.fixture(scope="module")
def helper(tmp_path_factory: pytest.TempPathFactory) -> Iterator[HelperClient]:
    """Client for a module-wide helper server that allows the current UID."""
    socket_dir = tmp_path_factory.mktemp("files-helper")
    with running_helper(socket_dir / "helper.sock", {os.geteuid()}) as client:
        yield client


def call_err(helper: HelperClient, action: str, args: dict[str, Any]) -> str:
    """Run an action expected to fail; return its helper error code."""
    with pytest.raises(HelperActionError) as excinfo:
        helper.call(action, args)
    return excinfo.value.helper_code


def read_fd_bytes(helper: HelperClient, path: str) -> tuple[dict[str, Any], bytes]:
    """files.read_fd result plus the bytes read from the passed descriptor."""
    result, fds = helper.call_with_fds("files.read_fd", {"path": path})
    try:
        assert len(fds) == 1
        data = b""
        while True:
            chunk = os.read(fds[0], 65536)
            if not chunk:
                break
            data += chunk
    finally:
        close_fds(fds)
    return result, data


def stage(helper: HelperClient, dir_path: str, data: bytes) -> str:
    """files.open_write + write data through the passed fd; returns staged_path."""
    result, fds = helper.call_with_fds("files.open_write", {"dir": dir_path})
    try:
        assert len(fds) == 1
        os.write(fds[0], data)
    finally:
        close_fds(fds)
    staged = result["staged_path"]
    assert isinstance(staged, str)
    return staged


def install(
    helper: HelperClient,
    staged: str,
    target: str,
    *,
    expected_etag: str | None = None,
    mode: int | None = None,
) -> dict[str, Any]:
    return helper.call(
        "files.install",
        {"staged_path": staged, "target": target, "expected_etag": expected_etag, "mode": mode},
    )


def etag_of(path: Path) -> str:
    st = path.lstat()
    return f"{st.st_size}-{st.st_mtime_ns}"


def build_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def build_tar(path: Path, members: dict[str, bytes], *, gz: bool = False) -> None:
    with tarfile.open(path, "w:gz" if gz else "w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


# ---------------------------------------------------------------------------
# path validation (traversal, NUL, //, trailing slash)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["relative/path", "/tmp/..", "/tmp/../etc", "//tmp", "/tmp//x", "/tmp/", "/tmp/\0x", ""],
    ids=["relative", "dotdot", "dotdot-mid", "double-slash", "dup-sep", "trailing", "nul", "empty"],
)
def test_invalid_paths_rejected_everywhere(helper: HelperClient, bad: str) -> None:
    assert call_err(helper, "files.read_fd", {"path": bad}) == "helper.path_invalid"
    assert call_err(helper, "files.delete", {"path": bad}) == "helper.path_invalid"
    assert call_err(helper, "files.mkdir", {"path": bad}) == "helper.path_invalid"


# ---------------------------------------------------------------------------
# files.read_fd
# ---------------------------------------------------------------------------


def test_read_fd_roundtrip(helper: HelperClient, tmp_path: Path) -> None:
    p = tmp_path / "hello.txt"
    p.write_bytes(b"hello files")
    result, data = read_fd_bytes(helper, str(p))
    assert data == b"hello files"
    assert result["size"] == 11
    assert result["etag"] == etag_of(p)


def test_read_fd_rejects_directory(helper: HelperClient, tmp_path: Path) -> None:
    assert call_err(helper, "files.read_fd", {"path": str(tmp_path)}) == "helper.unsupported"


def test_read_fd_rejects_fifo_without_hanging(helper: HelperClient, tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert call_err(helper, "files.read_fd", {"path": str(fifo)}) == "helper.unsupported"


def test_read_fd_rejects_symlink(helper: HelperClient, tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert call_err(helper, "files.read_fd", {"path": str(link)}) == "helper.denied"


def test_read_fd_missing_is_not_found(helper: HelperClient, tmp_path: Path) -> None:
    assert call_err(helper, "files.read_fd", {"path": str(tmp_path / "nope")}) == (
        "helper.path_not_found"
    )


# ---------------------------------------------------------------------------
# files.open_write + files.install
# ---------------------------------------------------------------------------


def test_stage_and_install_creates_file(helper: HelperClient, tmp_path: Path) -> None:
    target = tmp_path / "new.conf"
    staged = stage(helper, str(tmp_path), b"key=value\n")
    assert os.path.basename(staged).startswith(".ownpanel-tmp-")
    result = install(helper, staged, str(target))
    assert target.read_bytes() == b"key=value\n"
    assert result["etag"] == etag_of(target)
    assert not os.path.exists(staged)


def test_install_preserves_mode_and_supports_expected_etag(
    helper: HelperClient, tmp_path: Path
) -> None:
    target = tmp_path / "app.conf"
    target.write_text("v1")
    target.chmod(0o600)
    first = install(helper, stage(helper, str(tmp_path), b"v2"), str(target))
    assert target.read_text() == "v2"
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600  # mode survived the replace
    # Optimistic-concurrency happy path: correct expected_etag installs fine.
    second = install(
        helper, stage(helper, str(tmp_path), b"v3"), str(target), expected_etag=first["etag"]
    )
    assert target.read_text() == "v3"
    assert second["etag"] == etag_of(target)


def test_install_stale_etag_conflicts_and_cleans_staging(
    helper: HelperClient, tmp_path: Path
) -> None:
    target = tmp_path / "app.conf"
    target.write_text("their edit")
    staged = stage(helper, str(tmp_path), b"my edit")
    with pytest.raises(HelperActionError) as excinfo:
        install(helper, staged, str(target), expected_etag="0-0")
    assert excinfo.value.helper_code == "helper.conflict"
    assert target.read_text() == "their edit"  # target untouched
    assert not os.path.exists(staged)  # staging file cleaned up


def test_install_expected_etag_on_missing_target_conflicts(
    helper: HelperClient, tmp_path: Path
) -> None:
    staged = stage(helper, str(tmp_path), b"data")
    with pytest.raises(HelperActionError) as excinfo:
        install(helper, staged, str(tmp_path / "gone.txt"), expected_etag="1-1")
    assert excinfo.value.helper_code == "helper.conflict"


def test_install_refuses_symlink_target(helper: HelperClient, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("safe")
    link = tmp_path / "evil-link"
    link.symlink_to(victim)
    staged = stage(helper, str(tmp_path), b"pwned")
    with pytest.raises(HelperActionError) as excinfo:
        install(helper, staged, str(link))
    assert excinfo.value.helper_code == "helper.denied"
    assert victim.read_text() == "safe"
    assert link.is_symlink()


def test_install_refuses_foreign_staged_paths(helper: HelperClient, tmp_path: Path) -> None:
    src = tmp_path / "not-staged.txt"
    src.write_text("x")
    # Not an open_write product: must not be usable as a rename source.
    args = {"staged_path": str(src), "target": str(tmp_path / "t")}
    assert call_err(helper, "files.install", args) == "helper.denied"
    # Staged prefix but in a different directory than the target: also denied.
    other = tmp_path / "other"
    other.mkdir()
    staged = stage(helper, str(other), b"y")
    args = {"staged_path": staged, "target": str(tmp_path / "t")}
    assert call_err(helper, "files.install", args) == "helper.denied"


def test_open_write_errors(helper: HelperClient, tmp_path: Path) -> None:
    assert call_err(helper, "files.open_write", {"dir": str(tmp_path / "nope")}) == (
        "helper.path_not_found"
    )
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    assert call_err(helper, "files.open_write", {"dir": str(plain)}) == "helper.not_a_directory"


# ---------------------------------------------------------------------------
# the /proc /sys /dev write guard (every mutating action)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", ["/proc", "/sys", "/dev"])
def test_readonly_roots_deny_all_writes(helper: HelperClient, tmp_path: Path, root: str) -> None:
    inside = f"{root}/ownpanel-test"
    assert call_err(helper, "files.open_write", {"dir": root}) == "helper.denied"
    assert (
        call_err(
            helper,
            "files.install",
            {
                "staged_path": f"{root}/.ownpanel-tmp-x",
                "target": inside,
                "expected_etag": None,
                "mode": None,
            },
        )
        == "helper.denied"
    )
    assert call_err(helper, "files.mkdir", {"path": inside}) == "helper.denied"
    assert call_err(helper, "files.rename", {"src": inside, "dst": str(tmp_path / "o")}) == (
        "helper.denied"
    )
    assert call_err(helper, "files.rename", {"src": str(tmp_path), "dst": inside}) == (
        "helper.denied"
    )
    assert call_err(helper, "files.copy", {"src": str(tmp_path), "dst": inside}) == "helper.denied"
    assert call_err(helper, "files.delete", {"path": inside}) == "helper.denied"
    assert call_err(helper, "files.chmod", {"path": inside, "mode": 0o755}) == "helper.denied"
    assert call_err(helper, "files.chown", {"path": inside, "uid": -1, "gid": -1}) == (
        "helper.denied"
    )
    assert (
        call_err(
            helper,
            "files.archive",
            {"paths": [str(tmp_path)], "dst": f"{inside}.zip", "format": "zip"},
        )
        == "helper.denied"
    )
    assert (
        call_err(
            helper,
            "files.extract",
            {"archive": str(tmp_path / "a.zip"), "dst_dir": root, "format": "zip"},
        )
        == "helper.denied"
    )


def test_symlinked_parent_into_proc_is_denied(helper: HelperClient, tmp_path: Path) -> None:
    sneaky = tmp_path / "sneaky"
    sneaky.symlink_to("/proc")
    assert call_err(helper, "files.mkdir", {"path": str(sneaky / "x")}) == "helper.denied"
    assert call_err(helper, "files.open_write", {"dir": str(sneaky)}) == "helper.denied"


def test_delete_root_is_denied(helper: HelperClient) -> None:
    assert call_err(helper, "files.delete", {"path": "/", "recursive": True}) == "helper.denied"


# ---------------------------------------------------------------------------
# mkdir / rename / copy / delete
# ---------------------------------------------------------------------------


def test_mkdir_and_conflict_and_parents(helper: HelperClient, tmp_path: Path) -> None:
    d = tmp_path / "newdir"
    assert helper.call("files.mkdir", {"path": str(d)}) == {"ok": True}
    assert d.is_dir()
    assert call_err(helper, "files.mkdir", {"path": str(d)}) == "helper.conflict"
    nested = tmp_path / "a/b/c"
    assert helper.call("files.mkdir", {"path": str(nested), "parents": True}) == {"ok": True}
    assert nested.is_dir()


def test_rename(helper: HelperClient, tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("data")
    dst = tmp_path / "dst.txt"
    assert helper.call("files.rename", {"src": str(src), "dst": str(dst)}) == {"ok": True}
    assert dst.read_text() == "data" and not src.exists()
    other = tmp_path / "other.txt"
    other.write_text("x")
    assert call_err(helper, "files.rename", {"src": str(dst), "dst": str(other)}) == (
        "helper.conflict"
    )
    assert call_err(helper, "files.rename", {"src": str(tmp_path / "nope"), "dst": str(src)}) == (
        "helper.path_not_found"
    )


def test_copy_file_and_tree(helper: HelperClient, tmp_path: Path) -> None:
    src = tmp_path / "one.txt"
    src.write_text("copy me")
    dst = tmp_path / "two.txt"
    assert helper.call("files.copy", {"src": str(src), "dst": str(dst)}) == {"ok": True}
    assert dst.read_text() == "copy me"
    assert call_err(helper, "files.copy", {"src": str(src), "dst": str(dst)}) == "helper.conflict"

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "deep.txt").write_text("deep")
    assert call_err(helper, "files.copy", {"src": str(tree), "dst": str(tmp_path / "t2")}) == (
        "helper.unsupported"  # directories require recursive=True
    )
    assert helper.call(
        "files.copy", {"src": str(tree), "dst": str(tmp_path / "t2"), "recursive": True}
    ) == {"ok": True}
    assert (tmp_path / "t2" / "sub" / "deep.txt").read_text() == "deep"


def test_delete_variants(helper: HelperClient, tmp_path: Path) -> None:
    f = tmp_path / "gone.txt"
    f.write_text("x")
    assert helper.call("files.delete", {"path": str(f)}) == {"ok": True}
    assert not f.exists()
    assert call_err(helper, "files.delete", {"path": str(f)}) == "helper.path_not_found"

    d = tmp_path / "dir"
    (d / "kid").mkdir(parents=True)
    assert call_err(helper, "files.delete", {"path": str(d)}) == "helper.conflict"  # not empty
    assert helper.call("files.delete", {"path": str(d), "recursive": True}) == {"ok": True}
    assert not d.exists()


def test_delete_symlink_removes_link_not_target(helper: HelperClient, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("still here")
    link = tmp_path / "link"
    link.symlink_to(victim)
    assert helper.call("files.delete", {"path": str(link), "recursive": True}) == {"ok": True}
    assert not link.is_symlink()
    assert victim.read_text() == "still here"


# ---------------------------------------------------------------------------
# chmod / chown
# ---------------------------------------------------------------------------


def test_chmod(helper: HelperClient, tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    assert helper.call("files.chmod", {"path": str(f), "mode": 0o600}) == {"mode": 0o600}
    assert stat.S_IMODE(f.lstat().st_mode) == 0o600


def test_chmod_refuses_symlink(helper: HelperClient, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("x")
    victim.chmod(0o640)
    link = tmp_path / "link"
    link.symlink_to(victim)
    assert call_err(helper, "files.chmod", {"path": str(link), "mode": 0o777}) == "helper.denied"
    assert stat.S_IMODE(victim.lstat().st_mode) == 0o640  # never chmod'ed through the link


def test_chown_keep_and_explicit(helper: HelperClient, tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    kept = helper.call("files.chown", {"path": str(f)})  # defaults -1/-1 keep everything
    assert kept == {"uid": os.geteuid(), "gid": f.lstat().st_gid}
    explicit = helper.call(
        "files.chown", {"path": str(f), "uid": os.geteuid(), "gid": f.lstat().st_gid}
    )
    assert explicit == kept


# ---------------------------------------------------------------------------
# files.archive / files.extract
# ---------------------------------------------------------------------------


def make_tree(base: Path) -> Path:
    tree = base / "project"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.txt").write_text("alpha")
    (tree / "sub" / "b.txt").write_text("beta")
    return tree


@pytest.mark.parametrize("fmt", ["zip", "tar", "tar.gz"])
def test_archive_extract_roundtrip(helper: HelperClient, tmp_path: Path, fmt: str) -> None:
    tree = make_tree(tmp_path)
    dst = tmp_path / f"out.{fmt}"
    result = helper.call("files.archive", {"paths": [str(tree)], "dst": str(dst), "format": fmt})
    assert result["ok"] is True and dst.is_file()

    out = tmp_path / f"unpacked-{fmt.replace('.', '-')}"
    out.mkdir()
    extracted = helper.call(
        "files.extract", {"archive": str(dst), "dst_dir": str(out), "format": fmt}
    )
    assert extracted["entries"] >= 2
    assert (out / "project" / "a.txt").read_text() == "alpha"
    assert (out / "project" / "sub" / "b.txt").read_text() == "beta"


def test_archive_existing_dst_conflicts(helper: HelperClient, tmp_path: Path) -> None:
    tree = make_tree(tmp_path)
    dst = tmp_path / "out.zip"
    dst.write_text("occupied")
    assert (
        call_err(helper, "files.archive", {"paths": [str(tree)], "dst": str(dst), "format": "zip"})
        == "helper.conflict"
    )
    assert dst.read_text() == "occupied"


def test_archive_missing_source_not_found(helper: HelperClient, tmp_path: Path) -> None:
    assert (
        call_err(
            helper,
            "files.archive",
            {"paths": [str(tmp_path / "nope")], "dst": str(tmp_path / "o.zip"), "format": "zip"},
        )
        == "helper.path_not_found"
    )


@pytest.mark.parametrize("evil", ["../evil.txt", "a/../../evil.txt", "/tmp/evil-abs.txt"])
def test_extract_zip_slip_denied(helper: HelperClient, tmp_path: Path, evil: str) -> None:
    archive = tmp_path / "slip.zip"
    build_zip(archive, {evil: b"pwned"})
    dst = tmp_path / "dst"
    dst.mkdir()
    assert (
        call_err(
            helper, "files.extract", {"archive": str(archive), "dst_dir": str(dst), "format": "zip"}
        )
        == "helper.denied"
    )
    assert not (tmp_path / "evil.txt").exists()
    assert not os.path.exists("/tmp/evil-abs.txt")


def test_extract_tar_slip_denied(helper: HelperClient, tmp_path: Path) -> None:
    archive = tmp_path / "slip.tar"
    build_tar(archive, {"../evil.txt": b"pwned"})
    dst = tmp_path / "dst"
    dst.mkdir()
    assert (
        call_err(
            helper, "files.extract", {"archive": str(archive), "dst_dir": str(dst), "format": "tar"}
        )
        == "helper.denied"
    )
    assert not (tmp_path / "evil.txt").exists()


def test_extract_escape_through_preexisting_symlink_denied(
    helper: HelperClient, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "sub").symlink_to(outside)  # planted before extraction
    archive = tmp_path / "via-link.zip"
    build_zip(archive, {"sub/evil.txt": b"pwned"})
    assert (
        call_err(
            helper, "files.extract", {"archive": str(archive), "dst_dir": str(dst), "format": "zip"}
        )
        == "helper.denied"
    )
    assert list(outside.iterdir()) == []


def test_extract_skips_symlink_members(helper: HelperClient, tmp_path: Path) -> None:
    archive = tmp_path / "links.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ok.txt", "fine")
        info = zipfile.ZipInfo("evil-link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    dst = tmp_path / "dst"
    dst.mkdir()
    result = helper.call(
        "files.extract", {"archive": str(archive), "dst_dir": str(dst), "format": "zip"}
    )
    assert result == {"entries": 1, "skipped": 1}
    assert (dst / "ok.txt").read_text() == "fine"
    assert not (dst / "evil-link").exists() and not (dst / "evil-link").is_symlink()


def test_extract_skips_tar_symlink_members(helper: HelperClient, tmp_path: Path) -> None:
    archive = tmp_path / "links.tar"
    with tarfile.open(archive, "w") as tf:
        data = b"fine"
        reg = tarfile.TarInfo("ok.txt")
        reg.size = len(data)
        tf.addfile(reg, io.BytesIO(data))
        link = tarfile.TarInfo("evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    dst = tmp_path / "dst"
    dst.mkdir()
    result = helper.call(
        "files.extract", {"archive": str(archive), "dst_dir": str(dst), "format": "tar"}
    )
    assert result == {"entries": 1, "skipped": 1}
    assert not (dst / "evil-link").is_symlink()


def test_extract_entry_count_bomb_guard(
    helper: HelperClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_actions, "_EXTRACT_MAX_ENTRIES", 2)
    archive = tmp_path / "many.zip"
    build_zip(archive, {f"f{i}.txt": b"x" for i in range(3)})
    dst = tmp_path / "dst"
    dst.mkdir()
    assert (
        call_err(
            helper, "files.extract", {"archive": str(archive), "dst_dir": str(dst), "format": "zip"}
        )
        == "helper.too_large"
    )


def test_extract_total_bytes_bomb_guard(
    helper: HelperClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_actions, "_EXTRACT_MAX_BYTES", 10)
    # tar exercises the streaming counter (no declared-size shortcut).
    archive = tmp_path / "big.tar"
    build_tar(archive, {"big.txt": b"y" * 100})
    dst = tmp_path / "dst"
    dst.mkdir()
    assert (
        call_err(
            helper, "files.extract", {"archive": str(archive), "dst_dir": str(dst), "format": "tar"}
        )
        == "helper.too_large"
    )
    assert not (dst / "big.txt").exists()  # partial output removed

    # zip additionally aborts early on the declared uncompressed total.
    zarchive = tmp_path / "big.zip"
    build_zip(zarchive, {"big.txt": b"y" * 100})
    assert (
        call_err(
            helper,
            "files.extract",
            {"archive": str(zarchive), "dst_dir": str(dst), "format": "zip"},
        )
        == "helper.too_large"
    )


def test_extract_corrupt_archive_unsupported(helper: HelperClient, tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"this is not a zip file")
    dst = tmp_path / "dst"
    dst.mkdir()
    assert (
        call_err(
            helper, "files.extract", {"archive": str(junk), "dst_dir": str(dst), "format": "zip"}
        )
        == "helper.unsupported"
    )


def test_extract_dst_must_be_directory(helper: HelperClient, tmp_path: Path) -> None:
    archive = tmp_path / "a.zip"
    build_zip(archive, {"x.txt": b"x"})
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    assert (
        call_err(
            helper,
            "files.extract",
            {"archive": str(archive), "dst_dir": str(plain), "format": "zip"},
        )
        == "helper.not_a_directory"
    )
