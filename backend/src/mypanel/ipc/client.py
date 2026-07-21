"""API-side helper client: one fresh connection per call, sync with async wrapper."""

from __future__ import annotations

import asyncio
import socket
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mypanel.errors import HelperActionError, HelperUnavailableError
from mypanel.ipc.fdpass import close_fds
from mypanel.ipc.framing import FramingError, recv_frame, send_frame
from mypanel.ipc.messages import HelperRequest, HelperResponse


class HelperClient:
    """Client for the root helper Unix socket.

    Raises HelperActionError when the helper reports a failure and
    HelperUnavailableError on connection, timeout, or protocol trouble.
    """

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    def call(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        timeout_s: float = 30.0,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke an action and return its result dict ({} when the helper omits it).

        Descriptors unexpectedly attached to the response are closed here.
        """
        result, fds = self._exchange(action, args, timeout_s=timeout_s, job_id=job_id)
        close_fds(fds)
        return result

    def call_with_fds(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        timeout_s: float = 30.0,
        job_id: str | None = None,
    ) -> tuple[dict[str, Any], list[int]]:
        """Like call() but also returns received descriptors; the caller must close them."""
        return self._exchange(action, args, timeout_s=timeout_s, job_id=job_id)

    async def acall(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        timeout_s: float = 30.0,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Async wrapper around call() via asyncio.to_thread."""
        return await asyncio.to_thread(self.call, action, args, timeout_s=timeout_s, job_id=job_id)

    def _exchange(
        self,
        action: str,
        args: dict[str, Any] | None,
        *,
        timeout_s: float,
        job_id: str | None,
    ) -> tuple[dict[str, Any], list[int]]:
        request = HelperRequest(
            id=str(uuid.uuid4()),
            action=action,
            args=dict(args) if args else {},
            job_id=job_id,
            timeout_s=timeout_s,
        )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout_s)
                sock.connect(str(self._socket_path))
                send_frame(sock, request.model_dump())
                payload, fds = recv_frame(sock)
        except (OSError, FramingError) as exc:
            raise HelperUnavailableError(f"helper call failed: {exc}") from exc
        try:
            response = HelperResponse.model_validate(payload)
        except ValidationError as exc:
            close_fds(fds)
            raise HelperUnavailableError("helper response failed validation") from exc
        if response.id != request.id:
            close_fds(fds)
            raise HelperUnavailableError("helper response id mismatch")
        if not response.ok:
            close_fds(fds)
            if response.error is None:
                raise HelperUnavailableError("helper failure response lacks error detail")
            raise HelperActionError(response.error.code, response.error.message)
        if response.fds != len(fds):
            close_fds(fds)
            raise HelperUnavailableError(
                f"helper declared {response.fds} fds but delivered {len(fds)}"
            )
        return response.result or {}, fds
