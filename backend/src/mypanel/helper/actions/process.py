"""Helper process actions: deliver SIGTERM/SIGKILL to a pid and renice it.

Handlers self-register via ``@register`` at import time; this module is imported
by ``helper/actions/__init__.py`` so registration happens on helper startup.
Only these two signals are supported, pids are always positive (process-group
semantics of ``os.kill`` with 0/negative pids must never be reachable), and the
panel's own process plus pid 1 are refused outright.
"""

from __future__ import annotations

import os
import signal
from typing import Any, Literal

from pydantic import BaseModel, Field

from mypanel.helper.registry import ActionError, register
from mypanel.ipc.messages import HELPER_DENIED, HELPER_NOT_FOUND

_SIGNALS: dict[str, signal.Signals] = {
    "SIGTERM": signal.SIGTERM,
    "SIGKILL": signal.SIGKILL,
}


class SignalArgs(BaseModel):
    # ge=1 keeps process-group kills unreachable: os.kill(0, ...) signals our own
    # group and negative pids target whole groups (-1 means "every process").
    pid: int = Field(ge=1)
    signal: Literal["SIGTERM", "SIGKILL"]


class ReniceArgs(BaseModel):
    pid: int = Field(ge=1)
    nice: int = Field(ge=-20, le=19)


@register("process.signal", SignalArgs)
def process_signal(args: SignalArgs) -> dict[str, Any]:
    """Send SIGTERM/SIGKILL to one process.

    Self-protection: pid 1 (init) and the panel's own process are refused with
    ``helper.denied``. ``os.getpid()`` is the helper daemon in production and
    the API/test process in the in-process test harness — both must survive.
    """
    if args.pid == 1:
        raise ActionError(HELPER_DENIED, "refusing to signal pid 1 (init)")
    if args.pid == os.getpid():
        raise ActionError(HELPER_DENIED, "refusing to signal the panel's own process")
    try:
        os.kill(args.pid, _SIGNALS[args.signal])
    except ProcessLookupError as exc:
        raise ActionError(HELPER_NOT_FOUND, f"no such process: {args.pid}") from exc
    except PermissionError as exc:
        raise ActionError(HELPER_DENIED, f"not permitted to signal pid {args.pid}") from exc
    return {"ok": True}


@register("process.renice", ReniceArgs)
def process_renice(args: ReniceArgs) -> dict[str, Any]:
    """Set a process's nice value; returns the value re-read from the kernel."""
    try:
        os.setpriority(os.PRIO_PROCESS, args.pid, args.nice)
        return {"nice": os.getpriority(os.PRIO_PROCESS, args.pid)}
    except ProcessLookupError as exc:
        raise ActionError(HELPER_NOT_FOUND, f"no such process: {args.pid}") from exc
    except PermissionError as exc:
        raise ActionError(HELPER_DENIED, f"not permitted to renice pid {args.pid}") from exc
