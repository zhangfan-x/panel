"""Regression test for the upload size bound (adversarial-review finding).

A multipart upload sent with Transfer-Encoding: chunked (no Content-Length)
must still be rejected once it exceeds ``upload_max_bytes`` — the body is bounded
by a wrapped ASGI receive before ``request.form()`` can spool it to disk. The
size guard fires before any helper call, so no helper is needed here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from conftest import API, OwnerContext, SettingsFactory, csrf_headers

from mypanel.config import Settings

_UPLOAD_LIMIT = 2048


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(limits={"upload_max_bytes": _UPLOAD_LIMIT})


def _multipart_body(boundary: str, path: str, payload: bytes) -> bytes:
    return b"".join(
        [
            f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n'.encode(),
            f"{path}\r\n".encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="big.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
            payload,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )


async def test_chunked_upload_over_limit_is_rejected(
    client: httpx.AsyncClient, owner: OwnerContext
) -> None:
    boundary = "----mypaneltest"
    # Body far larger than the limit + multipart slack, sent chunked (no
    # Content-Length): the wrapped receive must abort it before request.form()
    # can spool it to disk — no helper is contacted.
    payload = b"x" * (256 * 1024)
    body = _multipart_body(boundary, "/tmp/mypanel-upload-probe.bin", payload)

    async def stream() -> AsyncIterator[bytes]:
        for i in range(0, len(body), 512):
            yield body[i : i + 512]

    resp = await client.post(
        f"{API}/files/upload",
        content=stream(),
        headers={
            **csrf_headers(owner),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "request.too_large"
