"""structlog configuration: JSON to stderr in production, pretty console output in dev.

Stdlib logging (uvicorn, sqlalchemy, ...) is bridged through ProcessorFormatter on the
root logger so every log line goes through the same renderer.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor

_HANDLER_NAME = "ownpanel"


def setup_logging(*, dev: bool = False) -> None:
    """Configure structlog and stdlib logging (idempotent: replaces our own handler)."""
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: Processor = (
        structlog.dev.ConsoleRenderer() if dev else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.name = _HANDLER_NAME
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.name == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if dev else logging.INFO)

    # uvicorn installs its own handlers unless started with log_config=None; strip them
    # so its records propagate to the root handler instead of bypassing our renderer.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Thin wrapper around structlog.get_logger with a concrete return type."""
    if name is None:
        return structlog.stdlib.get_logger()
    return structlog.stdlib.get_logger(name)
