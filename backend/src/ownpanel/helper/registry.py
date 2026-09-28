"""Action registry: maps action names to (args model, handler) pairs.

Handlers self-register at import time via the `register` decorator and receive
a validated instance of their args model. A handler returns either a plain
result dict or an ActionOutcome when descriptors must ride on the response.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from ownpanel.ipc.messages import HELPER_INVALID_ARGS, HELPER_UNKNOWN_ACTION


class ActionError(Exception):
    """Business-level failure raised by handlers; carries a stable helper error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class ActionOutcome:
    """Result of a dispatched action: JSON payload plus descriptors to attach."""

    result: dict[str, Any]
    fds: list[int] = field(default_factory=list)


HandlerResult = dict[str, Any] | ActionOutcome


@dataclass(frozen=True, slots=True)
class _Registration:
    args_model: type[BaseModel]
    handler: Callable[[BaseModel], HandlerResult]


_REGISTRY: dict[str, _Registration] = {}


def register[ModelT: BaseModel](
    name: str, args_model: type[ModelT]
) -> Callable[[Callable[[ModelT], HandlerResult]], Callable[[ModelT], HandlerResult]]:
    """Decorator registering `handler` under `name`; args are validated with `args_model`."""

    def decorator(
        handler: Callable[[ModelT], HandlerResult],
    ) -> Callable[[ModelT], HandlerResult]:
        if name in _REGISTRY:
            raise ValueError(f"helper action {name!r} is already registered")
        # dispatch() only ever calls the handler with an `args_model` instance,
        # so widening the parameter type to BaseModel is safe.
        _REGISTRY[name] = _Registration(
            args_model=args_model,
            handler=cast(Callable[[BaseModel], HandlerResult], handler),
        )
        return handler

    return decorator


def registered_actions() -> tuple[str, ...]:
    """Sorted names of all registered actions (startup logging and tests)."""
    return tuple(sorted(_REGISTRY))


def _summarise_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)
    parts: list[str] = []
    for error in errors[:3]:
        loc = ".".join(str(item) for item in error["loc"]) or "args"
        parts.append(f"{loc}: {error['msg']}")
    summary = "; ".join(parts)
    if len(errors) > 3:
        summary += f" (+{len(errors) - 3} more)"
    return summary


def dispatch(action: str, raw_args: dict[str, Any]) -> ActionOutcome:
    """Validate args and run the handler for `action`, normalising to ActionOutcome.

    Raises ActionError with HELPER_UNKNOWN_ACTION / HELPER_INVALID_ARGS for
    lookup and validation failures; handler exceptions propagate to the caller
    (the server maps unexpected ones to helper.internal).
    """
    registration = _REGISTRY.get(action)
    if registration is None:
        raise ActionError(HELPER_UNKNOWN_ACTION, f"unknown action: {action}")
    try:
        args = registration.args_model.model_validate(raw_args)
    except ValidationError as exc:
        raise ActionError(
            HELPER_INVALID_ARGS, f"invalid args: {_summarise_validation_error(exc)}"
        ) from exc
    outcome = registration.handler(args)
    if isinstance(outcome, dict):
        return ActionOutcome(result=outcome)
    return outcome
