"""AppError problem+json serialisation and the stable error-code contract.

Error codes are released API surface (docs/code-plan.md section 11): the snapshot
below lists every class explicitly so any accidental change fails loudly. Adding a
new error class must update ERROR_CONTRACT on purpose.
"""

from __future__ import annotations

import json

import pytest

from ownpanel import errors
from ownpanel.errors import AppError, HelperActionError, RateLimitedError

# class -> (status, code, title); released values must never change.
ERROR_CONTRACT: dict[type[AppError], tuple[int, str, str]] = {
    errors.AppError: (500, "internal", "Internal Server Error"),
    errors.BadRequestError: (400, "request.bad", "Bad Request"),
    errors.BadHostError: (400, "request.bad_host", "Host Not Allowed"),
    errors.ValidationFailedError: (422, "request.validation", "Validation Failed"),
    errors.AuthRequiredError: (401, "auth.required", "Authentication Required"),
    errors.InvalidCredentialsError: (401, "auth.invalid_credentials", "Invalid Credentials"),
    errors.SessionStateError: (403, "auth.session_state", "Session State Not Permitted"),
    errors.ReauthRequiredError: (403, "auth.reauth_required", "Recent Re-Authentication Required"),
    errors.RateLimitedError: (429, "auth.rate_limited", "Too Many Attempts"),
    errors.CsrfError: (403, "csrf.invalid", "CSRF Validation Failed"),
    errors.BadOriginError: (403, "request.bad_origin", "Origin Not Allowed"),
    errors.ForbiddenError: (403, "forbidden", "Forbidden"),
    errors.NotFoundError: (404, "not_found", "Not Found"),
    errors.ConflictError: (409, "conflict", "Conflict"),
    errors.BootstrapError: (403, "bootstrap.invalid_token", "Invalid Bootstrap Token"),
    errors.BootstrapDoneError: (409, "bootstrap.already_done", "Owner Already Exists"),
    errors.WeakPasswordError: (422, "auth.weak_password", "Password Too Weak"),
    errors.HelperUnavailableError: (502, "helper.unavailable", "Helper Service Unavailable"),
    errors.HelperActionError: (400, "helper.action_failed", "Helper Action Failed"),
    errors.PayloadTooLargeError: (413, "request.too_large", "Payload Too Large"),
    errors.TerminalUnavailableError: (503, "terminal.unavailable", "Terminal Service Unavailable"),
    errors.DockerUnavailableError: (503, "docker.unavailable", "Docker Daemon Unavailable"),
}


def _instantiate(cls: type[AppError]) -> AppError:
    if cls is RateLimitedError:
        return RateLimitedError(7)
    if cls is HelperActionError:
        return HelperActionError("helper.internal", "boom")
    return cls()


# --- contract stability ------------------------------------------------------------


def test_contract_lists_every_error_class():
    """Any added/removed/renamed AppError subclass must update the snapshot on purpose."""
    in_module = {
        obj for obj in vars(errors).values() if isinstance(obj, type) and issubclass(obj, AppError)
    }
    assert in_module == set(ERROR_CONTRACT)


@pytest.mark.parametrize("cls", list(ERROR_CONTRACT), ids=lambda cls: cls.__name__)
def test_status_code_title_are_stable(cls: type[AppError]):
    status, code, title = ERROR_CONTRACT[cls]
    assert cls.status == status
    assert cls.code == code
    assert cls.title == title
    body = _instantiate(cls).problem()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    assert body["title"] == title
    json.dumps(body)  # every problem body must be JSON-serialisable


def test_error_codes_are_unique():
    codes = [cls.code for cls in ERROR_CONTRACT]
    assert len(codes) == len(set(codes))


# --- problem() shape ---------------------------------------------------------------


def test_problem_minimal_shape():
    assert AppError().problem() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "code": "internal",
    }


def test_problem_includes_detail_and_request_id_when_given():
    body = errors.NotFoundError("Session not found.").problem("req_0123456789ab")
    assert body == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "code": "not_found",
        "detail": "Session not found.",
        "request_id": "req_0123456789ab",
    }


def test_problem_omits_empty_detail_and_missing_request_id():
    body = AppError("").problem()
    assert "detail" not in body
    assert "request_id" not in body


def test_problem_merges_extra_fields():
    body = AppError("broken", extra={"hint": "try later"}).problem()
    assert body["hint"] == "try later"
    assert body["detail"] == "broken"
    assert body["code"] == "internal"


def test_exception_message_defaults_to_title():
    assert str(AppError()) == "Internal Server Error"
    assert str(errors.ConflictError("duplicate name")) == "duplicate name"


# --- specialised constructors ------------------------------------------------------


def test_rate_limited_error_carries_retry_after():
    exc = RateLimitedError(42)
    assert exc.retry_after_s == 42
    body = exc.problem()
    assert body["retry_after"] == 42
    assert body["status"] == 429
    assert body["code"] == "auth.rate_limited"
    assert "detail" not in body


def test_rate_limited_error_with_detail():
    body = RateLimitedError(1, "slow down").problem()
    assert body["retry_after"] == 1
    assert body["detail"] == "slow down"


def test_helper_action_error_carries_helper_code():
    exc = HelperActionError("helper.path_not_found", "No such path.")
    assert exc.helper_code == "helper.path_not_found"
    body = exc.problem()
    assert body["helper_code"] == "helper.path_not_found"
    assert body["detail"] == "No such path."
    assert body["code"] == "helper.action_failed"
