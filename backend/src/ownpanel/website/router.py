"""Website (nginx) API: visual site management, config test/reload and certificates.

Every nginx file lives under root-owned ``settings.website.nginx_dir`` and every
certificate under ``settings.website.cert_dir``, so all work is delegated to the
root helper's ``nginx.*`` actions. Those base directories are supplied to the
helper on every call from settings (the helper has no config); site/cert *names*
are additionally constrained here by a strict URL/body pattern and re-validated
helper-side. Mutating endpoints require CSRF and are audited (success committed
with the request, failures committed on the denied path). ``reload`` runs
``nginx -t`` first and only reloads when the config is valid. Response field
names are the frontend contract; do not rename them.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from ownpanel.audit import record_audit
from ownpanel.auth.deps import ActiveSessionDep, CurrentAuth, DbDep, SettingsDep, require_csrf
from ownpanel.config import Settings
from ownpanel.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    HelperActionError,
    NotFoundError,
)
from ownpanel.ipc.client import HelperClient
from ownpanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_INVALID_ARGS,
    HELPER_NOT_FOUND,
    HELPER_PATH_NOT_FOUND,
)

website_router = APIRouter(prefix="/website", tags=["website"])

# Mirrors the helper's site/cert name allowlist; a leading alphanumeric rules out
# "." / ".." and the charset rules out path separators.
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$"
_DOMAIN_PATTERN = r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]{0,253})?$"

NameParam = Annotated[str, Path(pattern=_NAME_PATTERN)]

_MAX_CONFIG_BYTES = 512 * 1024
_MAX_CERT_BYTES = 256 * 1024
_READ_TIMEOUT_S = 15.0
_ACTION_TIMEOUT_S = 35.0
_OPENSSL_TIMEOUT_S = 65.0

SiteToggle = Literal["enable", "disable"]


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class SiteWriteRequest(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN)
    content: str = Field(max_length=_MAX_CONFIG_BYTES)


class CertUploadRequest(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN)
    cert_pem: str = Field(min_length=1, max_length=_MAX_CERT_BYTES)
    key_pem: str = Field(min_length=1, max_length=_MAX_CERT_BYTES)


class SelfSignedRequest(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN)
    domain: str = Field(pattern=_DOMAIN_PATTERN, max_length=253)
    days: int = Field(default=365, ge=1, le=3650)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _helper(request: Request) -> HelperClient:
    helper: HelperClient = request.app.state.helper
    return helper


def _nginx_dir(settings: Settings) -> str:
    return os.path.normpath(str(settings.website.nginx_dir))


def _cert_dir(settings: Settings) -> str:
    return os.path.normpath(str(settings.website.cert_dir))


async def _call(
    request: Request, action: str, args: dict[str, Any], *, timeout_s: float
) -> dict[str, Any]:
    """Run a helper ``nginx.*`` action, mapping helper error codes to HTTP status.

    Raises:
        NotFoundError: helper reported the site/cert (or a path) is missing.
        ConflictError: helper reported a conflicting on-disk state.
        BadRequestError: helper rejected the arguments (e.g. non-PEM upload).
    """
    try:
        result: dict[str, Any] = await _helper(request).acall(action, args, timeout_s=timeout_s)
    except HelperActionError as exc:
        if exc.helper_code in (HELPER_NOT_FOUND, HELPER_PATH_NOT_FOUND):
            raise NotFoundError(exc.detail or "not found") from exc
        if exc.helper_code == HELPER_CONFLICT:
            raise ConflictError(exc.detail or "conflict") from exc
        if exc.helper_code == HELPER_INVALID_ARGS:
            raise BadRequestError(exc.detail or "invalid arguments") from exc
        raise
    return result


async def _audit(
    request: Request,
    db: DbDep,
    auth: CurrentAuth,
    action: str,
    target: str,
    *,
    result: str = "ok",
    error_code: str | None = None,
) -> None:
    await record_audit(
        db,
        resource="website",
        action=action,
        result=result,
        target=target,
        actor_user_id=auth.user.id,
        request=request,
        error_code=error_code,
        commit=True,
    )


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


@website_router.get("/sites")
async def list_sites(
    request: Request, _auth: ActiveSessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """All ``sites-available`` files with enabled flag and parsed server_name/listen."""
    return await _call(
        request, "nginx.list_sites", {"nginx_dir": _nginx_dir(settings)}, timeout_s=_READ_TIMEOUT_S
    )


@website_router.get("/sites/{name}")
async def read_site(
    name: NameParam, request: Request, _auth: ActiveSessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Raw config text of one site (404 when absent)."""
    return await _call(
        request,
        "nginx.read_site",
        {"nginx_dir": _nginx_dir(settings), "name": name},
        timeout_s=_READ_TIMEOUT_S,
    )


@website_router.post("/sites", dependencies=[Depends(require_csrf)])
async def write_site(
    body: SiteWriteRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Create or replace a site config under ``sites-available`` (does not reload)."""
    try:
        result = await _call(
            request,
            "nginx.write_site",
            {"nginx_dir": _nginx_dir(settings), "name": body.name, "content": body.content},
            timeout_s=_ACTION_TIMEOUT_S,
        )
    except AppError as exc:
        await _audit(
            request, db, auth, "website.write_site", body.name, result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, "website.write_site", body.name)
    return result


@website_router.post("/sites/{name}/{action}", dependencies=[Depends(require_csrf)])
async def toggle_site(
    name: NameParam,
    action: SiteToggle,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Enable or disable a site by creating/removing its ``sites-enabled`` symlink."""
    try:
        result = await _call(
            request,
            f"nginx.{action}",
            {"nginx_dir": _nginx_dir(settings), "name": name},
            timeout_s=_ACTION_TIMEOUT_S,
        )
    except AppError as exc:
        await _audit(
            request, db, auth, f"website.{action}", name, result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, f"website.{action}", name)
    return result


@website_router.delete("/sites/{name}", dependencies=[Depends(require_csrf)])
async def delete_site(
    name: NameParam,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Remove a site from ``sites-available`` (and its ``sites-enabled`` symlink)."""
    try:
        result = await _call(
            request,
            "nginx.delete_site",
            {"nginx_dir": _nginx_dir(settings), "name": name},
            timeout_s=_ACTION_TIMEOUT_S,
        )
    except AppError as exc:
        await _audit(
            request, db, auth, "website.delete_site", name, result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, "website.delete_site", name)
    return result


# ---------------------------------------------------------------------------
# Config test / reload / status
# ---------------------------------------------------------------------------


@website_router.post("/test", dependencies=[Depends(require_csrf)])
async def test_config(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """Run ``nginx -t`` and return ``{ok, output}`` (read-only; not audited)."""
    return await _call(request, "nginx.test", {}, timeout_s=_ACTION_TIMEOUT_S)


@website_router.post("/reload", dependencies=[Depends(require_csrf)])
async def reload(request: Request, auth: ActiveSessionDep, db: DbDep) -> dict[str, Any]:
    """Test the config, then reload nginx only if it is valid.

    When ``nginx -t`` fails the config is NOT applied: the test output is
    returned with ``ok=false`` (HTTP 200) so the operator can fix it, and the
    attempt is audited as denied.
    """
    try:
        test = await _call(request, "nginx.test", {}, timeout_s=_ACTION_TIMEOUT_S)
        if not test.get("ok"):
            await _audit(
                request,
                db,
                auth,
                "website.reload",
                "nginx",
                result="denied",
                error_code="website.config_invalid",
            )
            return {"ok": False, "reloaded": False, "output": test.get("output", "")}
        await _call(request, "nginx.reload", {}, timeout_s=_ACTION_TIMEOUT_S)
    except AppError as exc:
        await _audit(
            request, db, auth, "website.reload", "nginx", result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, "website.reload", "nginx")
    return {"ok": True, "reloaded": True, "output": test.get("output", "")}


@website_router.get("/status")
async def status(request: Request, _auth: ActiveSessionDep) -> dict[str, Any]:
    """nginx service state: active / enabled / version."""
    return await _call(request, "nginx.status", {}, timeout_s=_READ_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


@website_router.get("/certs")
async def list_certs(
    request: Request, _auth: ActiveSessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """List ``*.crt`` / ``*.pem`` certificates with parsed subject/expiry."""
    result = await _call(
        request, "nginx.list_certs", {"cert_dir": _cert_dir(settings)}, timeout_s=_ACTION_TIMEOUT_S
    )
    # The absolute cert directory lets the frontend build ssl_certificate paths.
    result["cert_dir"] = _cert_dir(settings)
    return result


@website_router.post("/certs", dependencies=[Depends(require_csrf)])
async def upload_cert(
    body: CertUploadRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Store an uploaded certificate + private key (``<name>.crt`` / ``<name>.key``)."""
    try:
        result = await _call(
            request,
            "nginx.write_cert",
            {
                "cert_dir": _cert_dir(settings),
                "name": body.name,
                "cert_pem": body.cert_pem,
                "key_pem": body.key_pem,
            },
            timeout_s=_ACTION_TIMEOUT_S,
        )
    except AppError as exc:
        await _audit(
            request, db, auth, "website.write_cert", body.name, result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, "website.write_cert", body.name)
    return result


@website_router.post("/certs/self-signed", dependencies=[Depends(require_csrf)])
async def generate_self_signed(
    body: SelfSignedRequest,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Generate a self-signed RSA-2048 certificate for ``domain``."""
    try:
        result = await _call(
            request,
            "nginx.generate_self_signed",
            {
                "cert_dir": _cert_dir(settings),
                "name": body.name,
                "domain": body.domain,
                "days": body.days,
            },
            timeout_s=_OPENSSL_TIMEOUT_S,
        )
    except AppError as exc:
        await _audit(
            request,
            db,
            auth,
            "website.generate_cert",
            body.name,
            result="denied",
            error_code=exc.code,
        )
        raise
    await _audit(request, db, auth, "website.generate_cert", body.name)
    return result


@website_router.delete("/certs/{name}", dependencies=[Depends(require_csrf)])
async def delete_cert(
    name: NameParam,
    request: Request,
    auth: ActiveSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Remove a certificate and its private key by name."""
    try:
        result = await _call(
            request,
            "nginx.delete_cert",
            {"cert_dir": _cert_dir(settings), "name": name},
            timeout_s=_ACTION_TIMEOUT_S,
        )
    except AppError as exc:
        await _audit(
            request, db, auth, "website.delete_cert", name, result="denied", error_code=exc.code
        )
        raise
    await _audit(request, db, auth, "website.delete_cert", name)
    return result
