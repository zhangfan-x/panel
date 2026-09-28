"""Application error hierarchy with stable error codes and RFC 9457 serialisation.

Error codes are part of the API contract: once released they must never change.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all expected application errors."""

    status: int = 500
    code: str = "internal"
    title: str = "Internal Server Error"

    def __init__(self, detail: str | None = None, *, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.extra = extra or {}

    def problem(self, request_id: str | None = None) -> dict[str, Any]:
        """RFC 9457 problem+json body."""
        body: dict[str, Any] = {
            "type": "about:blank",
            "title": self.title,
            "status": self.status,
            "code": self.code,
        }
        if self.detail:
            body["detail"] = self.detail
        if request_id:
            body["request_id"] = request_id
        body.update(self.extra)
        return body


class BadRequestError(AppError):
    status = 400
    code = "request.bad"
    title = "Bad Request"


class BadHostError(AppError):
    status = 400
    code = "request.bad_host"
    title = "Host Not Allowed"


class ValidationFailedError(AppError):
    status = 422
    code = "request.validation"
    title = "Validation Failed"


class AuthRequiredError(AppError):
    status = 401
    code = "auth.required"
    title = "Authentication Required"


class InvalidCredentialsError(AppError):
    status = 401
    code = "auth.invalid_credentials"
    title = "Invalid Credentials"


class SessionStateError(AppError):
    """Session exists but is in the wrong state for this endpoint."""

    status = 403
    code = "auth.session_state"
    title = "Session State Not Permitted"


class ReauthRequiredError(AppError):
    status = 403
    code = "auth.reauth_required"
    title = "Recent Re-Authentication Required"


class RateLimitedError(AppError):
    status = 429
    code = "auth.rate_limited"
    title = "Too Many Attempts"

    def __init__(self, retry_after_s: int, detail: str | None = None) -> None:
        super().__init__(detail, extra={"retry_after": retry_after_s})
        self.retry_after_s = retry_after_s


class CsrfError(AppError):
    status = 403
    code = "csrf.invalid"
    title = "CSRF Validation Failed"


class BadOriginError(AppError):
    status = 403
    code = "request.bad_origin"
    title = "Origin Not Allowed"


class ForbiddenError(AppError):
    status = 403
    code = "forbidden"
    title = "Forbidden"


class NotFoundError(AppError):
    status = 404
    code = "not_found"
    title = "Not Found"


class ConflictError(AppError):
    status = 409
    code = "conflict"
    title = "Conflict"


class BootstrapError(AppError):
    status = 403
    code = "bootstrap.invalid_token"
    title = "Invalid Bootstrap Token"


class BootstrapDoneError(AppError):
    status = 409
    code = "bootstrap.already_done"
    title = "Owner Already Exists"


class WeakPasswordError(AppError):
    status = 422
    code = "auth.weak_password"
    title = "Password Too Weak"


class HelperUnavailableError(AppError):
    status = 502
    code = "helper.unavailable"
    title = "Helper Service Unavailable"


class HelperActionError(AppError):
    """Helper returned an error response; carries the helper-side error code."""

    status = 400
    code = "helper.action_failed"
    title = "Helper Action Failed"

    def __init__(self, helper_code: str, message: str) -> None:
        super().__init__(message, extra={"helper_code": helper_code})
        self.helper_code = helper_code


class PayloadTooLargeError(AppError):
    status = 413
    code = "request.too_large"
    title = "Payload Too Large"


class TerminalUnavailableError(AppError):
    status = 503
    code = "terminal.unavailable"
    title = "Terminal Service Unavailable"


class DockerUnavailableError(AppError):
    status = 503
    code = "docker.unavailable"
    title = "Docker Daemon Unavailable"
