"""Helper action modules; importing this package registers every action."""

from __future__ import annotations

from mypanel.helper.actions import (
    composesvc,
    dockercfg,
    dockersvc,
    files,
    nginx,
    process,
    systemd,
    ufw,
)

__all__ = [
    "composesvc",
    "dockercfg",
    "dockersvc",
    "files",
    "nginx",
    "process",
    "systemd",
    "ufw",
]
