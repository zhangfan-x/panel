"""FastAPI application factory: middleware stack, exception handlers, routers, SPA.

Middleware order (outermost first): RequestContext -> SecurityHeaders ->
HostOrigin -> routes, so Host/Origin rejections still carry the request id and
the security headers. Shared state lives on ``app.state`` (see
``mypanel.auth.deps`` for the consumer-side contract): ``settings``,
``limiters``, ``helper``, plus ``engine``/``sessionmaker`` set by the lifespan.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

import mypanel
from mypanel.api import api_router
from mypanel.config import Settings, get_settings, set_settings
from mypanel.db import make_engine, make_sessionmaker, run_migrations
from mypanel.errors import AppError, RateLimitedError, ValidationFailedError
from mypanel.events import EventBus
from mypanel.ipc.client import HelperClient
from mypanel.jobs.manager import JobManager
from mypanel.logging import get_logger, setup_logging
from mypanel.metrics.sampler import MetricsSampler
from mypanel.middleware import (
    SECURITY_HEADERS,
    HostOriginMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from mypanel.proxy.scheduler import SubscriptionScheduler
from mypanel.proxy.tracker import ProxyTracker
from mypanel.security import SlidingWindowLimiter
from mypanel.terminal.manager import TerminalManager

_PROBLEM_MEDIA_TYPE = "application/problem+json"
_MAX_VALIDATION_ITEMS = 5

log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    await asyncio.to_thread(run_migrations, settings)
    # Generate/validate the server secret now so the first request cannot fail on it.
    _ = settings.secret_key
    engine = make_engine(settings)
    app.state.engine = engine
    sessionmaker = make_sessionmaker(engine)
    app.state.sessionmaker = sessionmaker

    events: EventBus = app.state.events
    metrics = MetricsSampler(sessionmaker, settings, events)
    jobs = JobManager(sessionmaker, settings, events)
    proxy_tracker = ProxyTracker(settings)
    proxy_scheduler = SubscriptionScheduler(settings)
    app.state.metrics = metrics
    app.state.jobs = jobs
    app.state.proxy_tracker = proxy_tracker
    app.state.proxy_scheduler = proxy_scheduler
    await metrics.start()
    await jobs.start()
    await proxy_tracker.start()
    await proxy_scheduler.start()
    try:
        yield
    finally:
        await proxy_scheduler.stop()
        await proxy_tracker.stop()
        await jobs.stop()
        await metrics.stop()
        await app.state.terminal.aclose()
        await engine.dispose()


class SpaStaticFiles(StaticFiles):
    """Static frontend with SPA fallback: unknown non-/api paths serve index.html."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not scope["path"].startswith("/api"):
                return await super().get_response("index.html", scope)
            raise


def _request_id(request: Request) -> str | None:
    request_id: str | None = getattr(request.state, "request_id", None)
    return request_id


def _problem_response(
    error: AppError, request: Request, headers: dict[str, str] | None = None
) -> JSONResponse:
    # The security headers and X-Request-Id are normally added by the middleware
    # stack, but Starlette's ServerErrorMiddleware sends the unhandled-exception
    # handler's response with the *raw* send, bypassing SecurityHeaders/RequestContext.
    # Stamp them on the response object here so even a 500 carries them (code-plan §6);
    # for handled errors the middleware simply overwrites with identical values.
    request_id = _request_id(request)
    merged: dict[str, str] = dict(SECURITY_HEADERS)
    if request_id is not None:
        merged["X-Request-Id"] = request_id
    if headers:
        merged.update(headers)
    return JSONResponse(
        error.problem(request_id),
        status_code=error.status,
        media_type=_PROBLEM_MEDIA_TYPE,
        headers=merged,
    )


def _validation_detail(exc: RequestValidationError) -> str:
    errors = exc.errors()
    parts = [
        "{}: {}".format(
            ".".join(str(piece) for piece in err.get("loc", ())) or "request",
            err.get("msg", "invalid"),
        )
        for err in errors[:_MAX_VALIDATION_ITEMS]
    ]
    if len(errors) > _MAX_VALIDATION_ITEMS:
        parts.append(f"(+{len(errors) - _MAX_VALIDATION_ITEMS} more)")
    return "; ".join(parts)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application; keeps the settings singleton in sync."""
    if settings is None:
        settings = get_settings()
    else:
        set_settings(settings)
    setup_logging(dev=settings.server.dev_http)

    dev = settings.server.dev_http
    app = FastAPI(
        title="mypanel",
        version=mypanel.__version__,
        lifespan=_lifespan,
        docs_url="/api/docs" if dev else None,
        openapi_url="/api/openapi.json" if dev else None,
        redoc_url=None,
    )

    app.state.settings = settings
    login_attempts = settings.limits.login_max_attempts
    app.state.limiters = {
        "login_ip": SlidingWindowLimiter(login_attempts, 60),
        "login_user": SlidingWindowLimiter(login_attempts, 60),
        "reauth": SlidingWindowLimiter(5, 60),
    }
    app.state.helper = HelperClient(settings.paths.helper_socket)
    app.state.events = EventBus()
    app.state.terminal = TerminalManager(settings)

    # add_middleware wraps outside the previous one: register innermost first.
    app.add_middleware(HostOriginMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = (
            {"Retry-After": str(exc.retry_after_s)} if isinstance(exc, RateLimitedError) else None
        )
        return _problem_response(exc, request, headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem_response(ValidationFailedError(_validation_detail(exc)), request)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Full traceback goes to the log only; the client sees an opaque problem body.
        log.exception("request.unhandled_error", path=request.url.path)
        return _problem_response(AppError(), request)

    app.include_router(api_router)

    static_dir = settings.paths.static_dir
    if static_dir is not None and static_dir.is_dir():
        app.mount("/", SpaStaticFiles(directory=static_dir, html=True), name="spa")

    return app
