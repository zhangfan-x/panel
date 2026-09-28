"""Files API endpoint tests (docs/code-plan-m2-m5.md §4.4).

Read/stat/list via the live helper socket, download with HTTP Range, the
etag-guarded content editor flow, streamed uploads with the size limit, the
CSRF/re-auth gates on mutations and the archive/extract job round-trip.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import API, OwnerContext, SettingsFactory, csrf_headers, db_scalar
from fastapi import FastAPI
from sqlalchemy import select
from test_helper import running_helper

from mypanel.config import Settings
from mypanel.db.models import AuditLog
from mypanel.files.router import _CONTENT_MAX_BYTES, _unique_arc_name
from mypanel.ipc.client import HelperClient

# Importing the actions package registers the files.* helper handlers.
importlib.import_module("mypanel.helper.actions")

FILES = f"{API}/files"
UPLOAD_MAX = 1024  # module override of limits.upload_max_bytes


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert an RFC 9457 problem+json response with the given status/code."""
    assert resp.status_code == status, f"expected {status}, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    return body


@pytest.fixture(scope="module")
def helper_socket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-wide helper socket path, shared by the server and the app settings."""
    return tmp_path_factory.mktemp("files-api-helper") / "helper.sock"


@pytest.fixture(scope="module")
def helper(helper_socket: Path) -> Iterator[HelperClient]:
    """Client for a module-wide helper server that allows the current UID."""
    with running_helper(helper_socket, {os.geteuid()}) as client:
        yield client


@pytest.fixture
def settings(make_settings: SettingsFactory, helper_socket: Path) -> Settings:
    """App settings: module helper socket + a tiny upload limit for the 413 test."""
    return make_settings(
        paths={"helper_socket": helper_socket}, limits={"upload_max_bytes": UPLOAD_MAX}
    )


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """Per-test workspace directory the helper operates on."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


async def wait_job(app: FastAPI, job_id: str, timeout_s: float = 15.0) -> dict[str, Any]:
    """Poll the job manager until the job reaches a terminal state."""
    job: dict[str, Any] | None = None
    for _ in range(int(timeout_s / 0.05)):
        job = await app.state.jobs.get(job_id)
        assert job is not None, "job row disappeared"
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in time: {job}")


async def do_reauth(client: httpx.AsyncClient, owner: OwnerContext) -> None:
    resp = await client.post(
        f"{API}/auth/reauth", json={"password": owner.password}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# list / stat
# ---------------------------------------------------------------------------


async def test_list_requires_auth(client: httpx.AsyncClient, ws: Path) -> None:
    resp = await client.get(f"{FILES}/list", params={"path": str(ws)})
    assert_problem(resp, 401, "auth.required")


async def test_list_and_stat(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    (ws / "a.txt").write_text("alpha")
    (ws / ".hidden").write_text("h")
    resp = await client.get(f"{FILES}/list", params={"path": str(ws)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(ws)
    assert [e["name"] for e in body["entries"]] == ["a.txt"]

    resp = await client.get(f"{FILES}/list", params={"path": str(ws), "show_hidden": "true"})
    assert [e["name"] for e in resp.json()["entries"]] == [".hidden", "a.txt"]

    resp = await client.get(f"{FILES}/stat", params={"path": str(ws / "a.txt")})
    assert resp.status_code == 200
    st = resp.json()
    assert st["path"] == str(ws / "a.txt")
    assert st["type"] == "file"
    assert st["size"] == 5


@pytest.mark.parametrize(
    "bad",
    ["../etc", "/etc/../etc", "//etc", "/etc/", "/tmp/\0x"],
    ids=["relative", "dotdot", "double-slash", "trailing", "nul"],
)
async def test_list_path_traversal_rejected(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, bad: str
) -> None:
    resp = await client.get(f"{FILES}/list", params={"path": bad})
    body = assert_problem(resp, 400, "helper.action_failed")
    assert body["helper_code"] == "helper.path_invalid"


# ---------------------------------------------------------------------------
# download (fd streaming + HTTP Range)
# ---------------------------------------------------------------------------


async def test_download_requires_auth(client: httpx.AsyncClient, ws: Path) -> None:
    resp = await client.get(f"{FILES}/download", params={"path": str(ws / "x")})
    assert_problem(resp, 401, "auth.required")


async def test_download_full(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    payload = b"0123456789abcdef"
    f = ws / "blob.bin"
    f.write_bytes(payload)
    resp = await client.get(f"{FILES}/download", params={"path": str(f)})
    assert resp.status_code == 200
    assert resp.content == payload
    assert resp.headers["content-length"] == str(len(payload))
    assert resp.headers["accept-ranges"] == "bytes"
    assert 'filename="blob.bin"' in resp.headers["content-disposition"]
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_download_range(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    payload = b"0123456789"
    f = ws / "ranged.bin"
    f.write_bytes(payload)
    resp = await client.get(
        f"{FILES}/download", params={"path": str(f)}, headers={"Range": "bytes=2-5"}
    )
    assert resp.status_code == 206
    assert resp.content == b"2345"
    assert resp.headers["content-range"] == f"bytes 2-5/{len(payload)}"
    assert resp.headers["content-length"] == "4"

    resp = await client.get(
        f"{FILES}/download", params={"path": str(f)}, headers={"Range": "bytes=-4"}
    )
    assert resp.status_code == 206
    assert resp.content == b"6789"
    assert resp.headers["content-range"] == f"bytes 6-9/{len(payload)}"


async def test_download_unsatisfiable_range(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    f = ws / "small.bin"
    f.write_bytes(b"tiny")
    resp = await client.get(
        f"{FILES}/download", params={"path": str(f)}, headers={"Range": "bytes=100-"}
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */4"


async def test_download_missing_is_404(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    resp = await client.get(f"{FILES}/download", params={"path": str(ws / "nope.bin")})
    assert_problem(resp, 404, "not_found")


async def test_download_inline_image(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    payload = b"\x89PNG\r\n\x1a\nnot-a-real-png-but-bytes-are-bytes"
    f = ws / "pic.PNG"  # extension matching must be case-insensitive
    f.write_bytes(payload)
    resp = await client.get(f"{FILES}/download", params={"path": str(f), "inline": "1"})
    assert resp.status_code == 200
    assert resp.content == payload
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"].startswith("inline;")


async def test_download_inline_only_for_whitelisted_extensions(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    for name in ("page.html", "vector.svg", "noext"):
        f = ws / name
        f.write_bytes(b"<svg onload=alert(1)>")
        resp = await client.get(f"{FILES}/download", params={"path": str(f), "inline": "1"})
        assert resp.status_code == 200
        # Anything not on the image whitelist stays a generic attachment.
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.headers["content-disposition"].startswith("attachment;")


async def test_download_directory_streams_zip(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    root = ws / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "a.txt").write_bytes(b"alpha")
    (root / ".hidden").write_bytes(b"dot")
    (root / "sub" / "b.bin").write_bytes(os.urandom(3 * 256 * 1024))  # spans several chunks
    (root / "link").symlink_to(root / "a.txt")

    resp = await client.get(f"{FILES}/download", params={"path": str(root)})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="proj.zip"' in resp.headers["content-disposition"]
    assert "content-length" not in resp.headers  # streamed, size unknown up front

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.testzip() is None
    assert set(zf.namelist()) == {
        "proj/",
        "proj/.hidden",
        "proj/a.txt",
        "proj/empty/",
        "proj/sub/",
        "proj/sub/b.bin",
    }  # the symlink is never archived
    assert zf.read("proj/a.txt") == b"alpha"
    assert zf.read("proj/sub/b.bin") == (root / "sub" / "b.bin").read_bytes()
    mode = (zf.getinfo("proj/a.txt").external_attr >> 16) & 0o7777
    assert mode == (root / "a.txt").stat().st_mode & 0o7777


async def test_download_special_file_is_rejected(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    fifo = ws / "pipe"
    os.mkfifo(fifo)
    resp = await client.get(f"{FILES}/download", params={"path": str(fifo)})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# multi-entry download (streamed zip of a selection)
# ---------------------------------------------------------------------------


async def test_download_zip_packs_selected_files_and_dirs(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    (ws / "docs" / "deep").mkdir(parents=True)
    (ws / "docs" / "readme.md").write_bytes(b"# hi")
    (ws / "docs" / "deep" / "n.txt").write_bytes(b"nested")
    (ws / "a.txt").write_bytes(b"alpha")
    (ws / "skipped.txt").write_bytes(b"not selected")

    resp = await client.get(
        f"{FILES}/download-zip", params={"path": str(ws), "name": ["a.txt", "docs"]}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert f'filename="{ws.name}.zip"' in resp.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.testzip() is None
    assert set(zf.namelist()) == {
        "a.txt",
        "docs/",
        "docs/deep/",
        "docs/deep/n.txt",
        "docs/readme.md",
    }
    assert zf.read("a.txt") == b"alpha"
    assert zf.read("docs/deep/n.txt") == b"nested"


async def test_download_zip_rejects_traversal_in_names(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    """Names are bare entries, so a separator or ``..`` never reaches the helper."""
    for bad in ("..", "../etc/passwd", "sub/x", "/etc/passwd"):
        resp = await client.get(f"{FILES}/download-zip", params={"path": str(ws), "name": [bad]})
        assert resp.status_code == 400, f"{bad!r} should be rejected"


async def test_download_zip_skips_missing_and_404s_when_empty(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    (ws / "here.txt").write_bytes(b"present")
    # A stale selection still downloads whatever survives.
    resp = await client.get(
        f"{FILES}/download-zip", params={"path": str(ws), "name": ["here.txt", "gone.txt"]}
    )
    assert resp.status_code == 200
    assert set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist()) == {"here.txt"}

    resp = await client.get(f"{FILES}/download-zip", params={"path": str(ws), "name": ["gone.txt"]})
    assert resp.status_code == 404


def test_unique_arc_name_disambiguates_collisions() -> None:
    """Guards `_zip_stream` against same-named roots (entries in one directory
    are unique, so this only matters for callers mixing several directories)."""
    taken: set[str] = set()
    assert _unique_arc_name("a.txt", taken) == "a.txt"
    assert _unique_arc_name("a.txt", taken) == "a (2).txt"
    assert _unique_arc_name("a.txt", taken) == "a (3).txt"
    assert _unique_arc_name("docs", taken) == "docs"
    assert _unique_arc_name("docs", taken) == "docs (2)"


async def test_download_zip_requires_auth(client: httpx.AsyncClient, ws: Path) -> None:
    resp = await client.get(f"{FILES}/download-zip", params={"path": str(ws), "name": ["x"]})
    assert resp.status_code == 401


async def test_download_zip_caps_selection_size(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    names = [f"f{i}.txt" for i in range(101)]
    resp = await client.get(f"{FILES}/download-zip", params={"path": str(ws), "name": names})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# content editor flow (GET preview / PUT with etag)
# ---------------------------------------------------------------------------


async def test_content_roundtrip(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    target = ws / "notes.md"
    resp = await client.put(
        f"{FILES}/content",
        json={"path": str(target), "content": "# hello\n"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
    etag = resp.json()["etag"]
    assert target.read_text() == "# hello\n"

    resp = await client.get(f"{FILES}/content", params={"path": str(target)})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"content": "# hello\n", "etag": etag, "truncated": False}

    resp = await client.put(
        f"{FILES}/content",
        json={"path": str(target), "content": "# edited\n", "expected_etag": etag},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
    assert target.read_text() == "# edited\n"


async def test_content_put_conflict_on_stale_etag(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    target = ws / "conf.ini"
    resp = await client.put(
        f"{FILES}/content",
        json={"path": str(target), "content": "mine"},
        headers=csrf_headers(owner),
    )
    stale = resp.json()["etag"]
    target.write_text("changed externally")  # bumps size/mtime => new etag
    resp = await client.put(
        f"{FILES}/content",
        json={"path": str(target), "content": "clobber", "expected_etag": stale},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 409, "conflict")
    assert target.read_text() == "changed externally"
    denied = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "files.write", AuditLog.result == "denied"),
    )
    assert denied is not None
    assert denied.error_code == "conflict"
    # No staging litter left behind after the failed install.
    assert not [n for n in os.listdir(ws) if n.startswith(".mypanel-tmp-")]


async def test_content_get_too_large(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    big = ws / "big.log"
    big.write_bytes(b"x" * (_CONTENT_MAX_BYTES + 1))
    resp = await client.get(f"{FILES}/content", params={"path": str(big)})
    assert_problem(resp, 413, "request.too_large")


async def test_content_put_requires_csrf(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    target = ws / "no-csrf.txt"
    resp = await client.put(f"{FILES}/content", json={"path": str(target), "content": "x"})
    assert_problem(resp, 403, "csrf.invalid")
    assert not target.exists()


# ---------------------------------------------------------------------------
# upload (multipart, streamed, size-capped)
# ---------------------------------------------------------------------------


async def test_upload_ok(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    target = ws / "uploaded.bin"
    payload = b"binary \x00 payload"
    resp = await client.post(
        f"{FILES}/upload",
        data={"path": str(target)},
        files={"file": ("uploaded.bin", payload)},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(target)
    assert target.read_bytes() == payload
    st = target.lstat()
    assert body["etag"] == f"{st.st_size}-{st.st_mtime_ns}"
    ok_row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "files.upload", AuditLog.result == "ok"),
    )
    assert ok_row is not None
    assert ok_row.target == str(target)


async def test_upload_too_large_is_413(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    target = ws / "huge.bin"
    resp = await client.post(
        f"{FILES}/upload",
        data={"path": str(target)},
        files={"file": ("huge.bin", b"z" * (UPLOAD_MAX * 2))},
        headers=csrf_headers(owner),
    )
    assert_problem(resp, 413, "request.too_large")
    assert not target.exists()
    # The staging file was cleaned up after the aborted stream.
    assert not [n for n in os.listdir(ws) if n.startswith(".mypanel-tmp-")]


async def test_upload_requires_csrf(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    resp = await client.post(
        f"{FILES}/upload",
        data={"path": str(ws / "x.bin")},
        files={"file": ("x.bin", b"data")},
    )
    assert_problem(resp, 403, "csrf.invalid")


# ---------------------------------------------------------------------------
# mkdir / rename / copy / chmod / chown
# ---------------------------------------------------------------------------


async def test_mkdir_rename_copy_chmod_chown_flow(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    d = ws / "made"
    resp = await client.post(f"{FILES}/mkdir", json={"path": str(d)}, headers=csrf_headers(owner))
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert d.is_dir()

    src = ws / "one.txt"
    src.write_text("move me")
    dst = ws / "made" / "two.txt"
    resp = await client.post(
        f"{FILES}/rename", json={"src": str(src), "dst": str(dst)}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    assert dst.read_text() == "move me" and not src.exists()

    copied = ws / "three.txt"
    resp = await client.post(
        f"{FILES}/copy", json={"src": str(dst), "dst": str(copied)}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert copied.read_text() == "move me"

    resp = await client.post(
        f"{FILES}/chmod", json={"path": str(copied), "mode": 0o600}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200 and resp.json() == {"mode": 0o600}

    resp = await client.post(
        f"{FILES}/chown", json={"path": str(copied)}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200
    assert resp.json() == {"uid": os.geteuid(), "gid": copied.lstat().st_gid}


async def test_mkdir_conflict_is_409(
    client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    resp = await client.post(f"{FILES}/mkdir", json={"path": str(ws)}, headers=csrf_headers(owner))
    assert_problem(resp, 409, "conflict")


async def test_mkdir_requires_csrf(
    client: httpx.AsyncClient, owner: OwnerContext, ws: Path
) -> None:
    resp = await client.post(f"{FILES}/mkdir", json={"path": str(ws / "d")})
    assert_problem(resp, 403, "csrf.invalid")
    assert not (ws / "d").exists()


# ---------------------------------------------------------------------------
# DELETE /files: CSRF + recent re-auth gates
# ---------------------------------------------------------------------------


async def test_delete_gates_and_success(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    victim = ws / "victim.txt"
    victim.write_text("delete me")

    resp = await client.request("DELETE", FILES, json={"path": str(victim)})
    assert_problem(resp, 403, "csrf.invalid")
    assert victim.exists()

    # CSRF passes but the fresh session has no recent re-auth.
    resp = await client.request(
        "DELETE", FILES, json={"path": str(victim)}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 403, "auth.reauth_required")
    assert victim.exists()

    await do_reauth(client, owner)
    resp = await client.request(
        "DELETE", FILES, json={"path": str(victim)}, headers=csrf_headers(owner)
    )
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert not victim.exists()

    ok_row = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "files.delete", AuditLog.result == "ok"),
    )
    assert ok_row is not None
    assert ok_row.target == str(victim)


async def test_delete_missing_is_404(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    await do_reauth(client, owner)
    resp = await client.request(
        "DELETE", FILES, json={"path": str(ws / "ghost")}, headers=csrf_headers(owner)
    )
    assert_problem(resp, 404, "not_found")
    denied = await db_scalar(
        app,
        select(AuditLog).where(AuditLog.action == "files.delete", AuditLog.result == "denied"),
    )
    assert denied is not None
    assert denied.error_code == "not_found"


# ---------------------------------------------------------------------------
# archive / extract jobs (202 + job manager)
# ---------------------------------------------------------------------------


async def test_archive_then_extract_jobs(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    tree = ws / "proj"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "deep.txt").write_text("deep data")
    dst = ws / "proj.zip"

    resp = await client.post(
        f"{FILES}/archive",
        json={"paths": [str(tree)], "dst": str(dst), "format": "zip"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 202
    job = await wait_job(app, resp.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert dst.is_file()

    out = ws / "unpacked"
    out.mkdir()
    resp = await client.post(
        f"{FILES}/extract",
        json={"archive": str(dst), "dst_dir": str(out), "format": "zip"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 202
    job = await wait_job(app, resp.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert (out / "proj" / "sub" / "deep.txt").read_text() == "deep data"


async def test_extract_zip_slip_job_fails(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    archive = ws / "slip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "pwned")
    out = ws / "safe"
    out.mkdir()
    resp = await client.post(
        f"{FILES}/extract",
        json={"archive": str(archive), "dst_dir": str(out), "format": "zip"},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 202
    job = await wait_job(app, resp.json()["job_id"])
    assert job["status"] == "failed", job
    assert not (ws / "evil.txt").exists()


async def test_archive_requires_csrf(
    client: httpx.AsyncClient, owner: OwnerContext, ws: Path
) -> None:
    resp = await client.post(
        f"{FILES}/archive",
        json={"paths": [str(ws)], "dst": str(ws / "a.zip"), "format": "zip"},
    )
    assert_problem(resp, 403, "csrf.invalid")


async def test_recursive_copy_runs_as_job(
    app: FastAPI, client: httpx.AsyncClient, owner: OwnerContext, helper: HelperClient, ws: Path
) -> None:
    tree = ws / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("copied")
    resp = await client.post(
        f"{FILES}/copy",
        json={"src": str(tree), "dst": str(ws / "tree2"), "recursive": True},
        headers=csrf_headers(owner),
    )
    assert resp.status_code == 202
    job = await wait_job(app, resp.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert (ws / "tree2" / "sub" / "f.txt").read_text() == "copied"
