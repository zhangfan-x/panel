"""Request/response models and stable error codes for the helper wire protocol."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Stable helper-side error codes; once released they must never change.
HELPER_UNKNOWN_ACTION = "helper.unknown_action"
HELPER_INVALID_ARGS = "helper.invalid_args"
HELPER_INTERNAL = "helper.internal"
HELPER_PATH_INVALID = "helper.path_invalid"
HELPER_PATH_NOT_FOUND = "helper.path_not_found"
HELPER_NOT_A_DIRECTORY = "helper.not_a_directory"
HELPER_DENIED = "helper.denied"
HELPER_TIMEOUT = "helper.timeout"
HELPER_PROTOCOL = "helper.protocol"


class HelperRequest(BaseModel):
    """One request frame sent from the API process to the helper."""

    v: int = 1
    id: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    job_id: str | None = None
    timeout_s: float = 30.0


class HelperError(BaseModel):
    """Error payload carried by a failed HelperResponse."""

    code: str
    message: str


class HelperResponse(BaseModel):
    """One response frame; `fds` declares how many descriptors ride on the frame."""

    v: int = 1
    id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: HelperError | None = None
    fds: int = 0
