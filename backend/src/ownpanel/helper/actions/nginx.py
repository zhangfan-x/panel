"""Helper nginx site/cert management actions (docs FEATURE: website module).

nginx config lives under root-owned ``/etc/nginx`` (standard Ubuntu layout:
``sites-available/`` holding real config files, ``sites-enabled/`` holding
symlinks that ``nginx.conf`` globs), so every read/write/enable/reload goes
through the root helper. TLS material lives under a dedicated ``cert_dir`` the
helper creates ``0750`` on demand.

The base directories (``nginx_dir`` / ``cert_dir``) are NOT user input: the API
passes them verbatim from ``settings.website`` on every call and the helper only
re-checks the generic absolute/normalised path rules (it has no settings, mirror
of the compose domain). Every *name* IS user input and is validated against a
strict filename allowlist before it is ever joined onto a base directory, so a
name can never contain ``/`` or ``..`` and therefore never escapes its directory.

External programs are always invoked by absolute path with an argument array and
``shell=False`` — nothing is interpolated into a shell string. Handlers
self-register via ``@register`` at import time; this module is imported by
``helper/actions/__init__.py`` so registration happens on helper startup.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import subprocess
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ownpanel.helper.actions.files import _map_os_errors, _open_nofollow, _validate_path
from ownpanel.helper.registry import ActionError, register
from ownpanel.ipc.messages import (
    HELPER_CONFLICT,
    HELPER_INTERNAL,
    HELPER_INVALID_ARGS,
    HELPER_NOT_FOUND,
    HELPER_TIMEOUT,
    HELPER_UNSUPPORTED,
)

# Fixed binary paths; tests monkeypatch these to fake CLIs where needed.
_NGINX = "/usr/sbin/nginx"
_SYSTEMCTL = "/usr/bin/systemctl"
_OPENSSL = "/usr/bin/openssl"

_SITES_AVAILABLE = "sites-available"
_SITES_ENABLED = "sites-enabled"

# Site file names and cert names: must be a plain filename (no path separators,
# no leading dot so ``.`` / ``..`` are impossible) using a conservative charset.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")
# Hostname / SAN for self-signed generation (optional leading wildcard label).
_DOMAIN_RE = re.compile(r"(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]{0,253})?")

_MAX_CONFIG_BYTES = 512 * 1024
_MAX_CERT_BYTES = 256 * 1024
_STAGED_PREFIX = ".ownpanel-nginx-"
_OUTPUT_TAIL_CHARS = 8000

_READ_TIMEOUT_S = 15.0
_ACTION_TIMEOUT_S = 30.0
_OPENSSL_TIMEOUT_S = 60.0

# openssl x509 -enddate prints e.g. ``notAfter=Aug 21 10:47:02 2026 GMT``.
_OPENSSL_DATE_FMT = "%b %d %H:%M:%S %Y"


# ---------------------------------------------------------------------------
# Argument models
# ---------------------------------------------------------------------------


class EmptyArgs(BaseModel):
    pass


class NginxDirArgs(BaseModel):
    nginx_dir: str


class SiteArgs(BaseModel):
    nginx_dir: str
    name: str


class WriteSiteArgs(BaseModel):
    nginx_dir: str
    name: str
    content: str = Field(max_length=_MAX_CONFIG_BYTES)


class CertDirArgs(BaseModel):
    cert_dir: str


class WriteCertArgs(BaseModel):
    cert_dir: str
    name: str
    cert_pem: str = Field(min_length=1, max_length=_MAX_CERT_BYTES)
    key_pem: str = Field(min_length=1, max_length=_MAX_CERT_BYTES)


class CertArgs(BaseModel):
    cert_dir: str
    name: str


class SelfSignedArgs(BaseModel):
    cert_dir: str
    name: str
    domain: str
    days: int = Field(default=365, ge=1, le=3650)


# ---------------------------------------------------------------------------
# Validation & small filesystem primitives
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> str:
    """Require a plain, dot-free-leading filename; returns it unchanged.

    Raises:
        ActionError: ``helper.invalid_args`` for anything containing ``/``,
            ``..``, control characters or otherwise off the allowlist.
    """
    if ".." in name or "/" in name or not _NAME_RE.fullmatch(name):
        raise ActionError(HELPER_INVALID_ARGS, f"invalid name: {name!r}")
    return name


def _validate_domain(domain: str) -> str:
    """Require a hostname-shaped domain (optional wildcard label) for the cert CN/SAN."""
    if len(domain) > 253 or not _DOMAIN_RE.fullmatch(domain):
        raise ActionError(HELPER_INVALID_ARGS, f"invalid domain: {domain!r}")
    return domain


def _ensure_dir(path: str, mode: int) -> None:
    """Create ``path`` (and parents) if absent, forcing ``mode`` on a fresh leaf.

    ``os.makedirs`` masks its ``mode`` with the process umask, so a freshly
    created directory is re-chmod'd to the exact mode; an existing directory is
    left untouched (its administrator may have chosen a different mode).
    """
    existed = os.path.isdir(path)
    with _map_os_errors(path):
        os.makedirs(path, mode=mode, exist_ok=True)
        if not existed:
            os.chmod(path, mode)


def _write_atomic(target: str, data: bytes, mode: int) -> None:
    """Write ``data`` to ``target`` via a staged temp file + fsync + atomic rename.

    The staging file is created ``O_CREAT|O_EXCL|O_NOFOLLOW`` in the target's own
    directory (same filesystem, so ``rename`` is atomic), fully written, fsync'd
    and renamed over the target; the parent directory is fsync'd so the rename is
    durable. Every failure path removes the staging file.
    """
    directory = os.path.dirname(target)
    staged = os.path.join(directory, _STAGED_PREFIX + secrets.token_hex(8))
    with _map_os_errors(directory):
        fd = _open_nofollow(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with suppress(OSError):
            os.unlink(staged)
        raise
    os.close(fd)
    try:
        os.chmod(staged, mode)  # O_CREAT mode is masked by umask; force it exactly
        os.rename(staged, target)
    except BaseException:
        with suppress(OSError):
            os.unlink(staged)
        raise
    with suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _read_text(path: str, cap: int) -> str:
    """Bounded, symlink-refusing read of a regular file, decoded as UTF-8."""
    with _map_os_errors(path):
        fd = _open_nofollow(path, os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ActionError(HELPER_UNSUPPORTED, f"not a regular file: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)[:cap].decode("utf-8", "replace")


def _run(argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run one command (absolute path, arg array, no shell) and capture output."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionError(HELPER_TIMEOUT, f"command timed out: {argv[0]}") from exc
    except OSError as exc:
        raise ActionError(HELPER_UNSUPPORTED, f"cannot run {argv[0]}: {exc}") from exc


def _tail(text: str) -> str:
    """Trailing, whitespace-trimmed slice of subprocess output (bounded for the wire)."""
    return text.strip()[-_OUTPUT_TAIL_CHARS:]


# ---------------------------------------------------------------------------
# Site directory helpers
# ---------------------------------------------------------------------------


def _available_path(nginx_dir: str, name: str) -> str:
    return os.path.join(nginx_dir, _SITES_AVAILABLE, name)


def _enabled_path(nginx_dir: str, name: str) -> str:
    return os.path.join(nginx_dir, _SITES_ENABLED, name)


def _lexists(path: str) -> bool:
    return os.path.lexists(path)


def _lstat_or_none(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


_SERVER_NAME_RE = re.compile(r"^\s*server_name\s+([^;]+);")
_LISTEN_RE = re.compile(r"^\s*listen\s+([^;]+);")
_PROXY_PASS_RE = re.compile(r"^\s*proxy_pass\s+([^;]+);")


def _parse_directives(text: str) -> tuple[list[str], list[str], list[str]]:
    """Best-effort extraction of ``server_name`` / ``listen`` / ``proxy_pass``.

    Comments are skipped. ``proxy_pass`` targets are the backend the site
    actually forwards to (the "real" application port); a static site has none.
    """
    server_names: list[str] = []
    listens: list[str] = []
    upstreams: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sn = _SERVER_NAME_RE.match(line)
        if sn:
            server_names.extend(tok for tok in sn.group(1).split() if tok)
        ls = _LISTEN_RE.match(line)
        if ls:
            tokens = ls.group(1).split()
            if tokens:
                listens.append(tokens[0])
        pp = _PROXY_PASS_RE.match(line)
        if pp:
            target = pp.group(1).strip()
            if target:
                upstreams.append(target)
    # De-duplicate while preserving order.
    return (
        list(dict.fromkeys(server_names)),
        list(dict.fromkeys(listens)),
        list(dict.fromkeys(upstreams)),
    )


@register("nginx.list_sites", NginxDirArgs)
def nginx_list_sites(args: NginxDirArgs) -> dict[str, Any]:
    """List ``sites-available`` files with enabled flag and parsed server_name/listen.

    A missing ``sites-available`` yields an empty list rather than an error: a
    fresh install (or an empty test directory) simply has no sites yet.
    """
    nginx_dir = _validate_path(args.nginx_dir)
    available_dir = os.path.join(nginx_dir, _SITES_AVAILABLE)
    enabled_dir = os.path.join(nginx_dir, _SITES_ENABLED)
    sites: list[dict[str, Any]] = []
    try:
        scan = os.scandir(available_dir)
    except (FileNotFoundError, NotADirectoryError):
        return {"sites": []}
    except PermissionError as exc:
        raise ActionError(HELPER_INTERNAL, f"cannot scan {available_dir}") from exc
    with scan:
        for entry in scan:
            if entry.name.startswith(".") or not _NAME_RE.fullmatch(entry.name):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            server_names: list[str] = []
            listens: list[str] = []
            upstreams: list[str] = []
            with suppress(ActionError, OSError):
                server_names, listens, upstreams = _parse_directives(
                    _read_text(os.path.join(available_dir, entry.name), _MAX_CONFIG_BYTES)
                )
            sites.append(
                {
                    "name": entry.name,
                    "enabled": _lexists(os.path.join(enabled_dir, entry.name)),
                    "server_names": server_names,
                    "listens": listens,
                    "upstreams": upstreams,
                }
            )
    sites.sort(key=lambda item: str(item["name"]))
    return {"sites": sites}


@register("nginx.read_site", SiteArgs)
def nginx_read_site(args: SiteArgs) -> dict[str, Any]:
    """Return the raw config text of one site (404 when absent)."""
    nginx_dir = _validate_path(args.nginx_dir)
    name = _validate_name(args.name)
    path = _available_path(nginx_dir, name)
    if not _lexists(path):
        raise ActionError(HELPER_NOT_FOUND, f"site not found: {name}")
    content = _read_text(path, _MAX_CONFIG_BYTES)
    return {"name": name, "content": content, "enabled": _lexists(_enabled_path(nginx_dir, name))}


@register("nginx.write_site", WriteSiteArgs)
def nginx_write_site(args: WriteSiteArgs) -> dict[str, Any]:
    """Create or replace a site config under ``sites-available`` (no reload)."""
    nginx_dir = _validate_path(args.nginx_dir)
    name = _validate_name(args.name)
    _ensure_dir(os.path.join(nginx_dir, _SITES_AVAILABLE), 0o755)
    target = _available_path(nginx_dir, name)
    existing = os.path.lexists(target)
    if existing and stat.S_ISDIR(os.lstat(target).st_mode):
        raise ActionError(HELPER_CONFLICT, f"site path is a directory: {name}")
    _write_atomic(target, args.content.encode("utf-8"), 0o644)
    return {"name": name, "created": not existing}


@register("nginx.enable", SiteArgs)
def nginx_enable(args: SiteArgs) -> dict[str, Any]:
    """Symlink ``sites-enabled/<name>`` → ``sites-available/<name>`` (idempotent)."""
    nginx_dir = _validate_path(args.nginx_dir)
    name = _validate_name(args.name)
    available = _available_path(nginx_dir, name)
    if not _lexists(available):
        raise ActionError(HELPER_NOT_FOUND, f"site not found: {name}")
    enabled_dir = os.path.join(nginx_dir, _SITES_ENABLED)
    _ensure_dir(enabled_dir, 0o755)
    link = os.path.join(enabled_dir, name)
    with _map_os_errors(link):
        st = _lstat_or_none(link)
        if st is not None:
            if stat.S_ISLNK(st.st_mode) and os.path.realpath(link) == os.path.realpath(available):
                return {"name": name, "enabled": True, "changed": False}
            raise ActionError(HELPER_CONFLICT, f"sites-enabled/{name} already exists")
        os.symlink(available, link)
    return {"name": name, "enabled": True, "changed": True}


@register("nginx.disable", SiteArgs)
def nginx_disable(args: SiteArgs) -> dict[str, Any]:
    """Remove the ``sites-enabled`` symlink for a site (idempotent).

    Only a symlink is ever removed; a real file placed directly in
    ``sites-enabled`` is refused (``helper.conflict``) rather than deleted.
    """
    nginx_dir = _validate_path(args.nginx_dir)
    name = _validate_name(args.name)
    link = _enabled_path(nginx_dir, name)
    st = _lstat_or_none(link)
    if st is None:
        return {"name": name, "enabled": False, "changed": False}
    if not stat.S_ISLNK(st.st_mode):
        raise ActionError(HELPER_CONFLICT, f"sites-enabled/{name} is not a symlink")
    with _map_os_errors(link):
        os.unlink(link)
    return {"name": name, "enabled": False, "changed": True}


@register("nginx.delete_site", SiteArgs)
def nginx_delete_site(args: SiteArgs) -> dict[str, Any]:
    """Remove a site from ``sites-available`` (and its ``sites-enabled`` symlink)."""
    nginx_dir = _validate_path(args.nginx_dir)
    name = _validate_name(args.name)
    available = _available_path(nginx_dir, name)
    link = _enabled_path(nginx_dir, name)
    link_st = _lstat_or_none(link)
    if link_st is not None and stat.S_ISLNK(link_st.st_mode):
        with suppress(OSError):
            os.unlink(link)
    st = _lstat_or_none(available)
    if st is None:
        raise ActionError(HELPER_NOT_FOUND, f"site not found: {name}")
    if stat.S_ISDIR(st.st_mode):
        raise ActionError(HELPER_CONFLICT, f"site path is a directory: {name}")
    with _map_os_errors(available):
        os.unlink(available)
    return {"name": name}


# ---------------------------------------------------------------------------
# nginx -t / reload / status
# ---------------------------------------------------------------------------


@register("nginx.test", EmptyArgs)
def nginx_test(args: EmptyArgs) -> dict[str, Any]:
    """Run ``nginx -t`` and report success plus its (stderr) output."""
    cp = _run([_NGINX, "-t"], timeout_s=_ACTION_TIMEOUT_S)
    output = cp.stderr.strip() or cp.stdout.strip()
    return {"ok": cp.returncode == 0, "output": _tail(output)}


@register("nginx.reload", EmptyArgs)
def nginx_reload(args: EmptyArgs) -> dict[str, Any]:
    """Reload nginx via ``systemctl reload nginx`` (caller tests the config first)."""
    cp = _run([_SYSTEMCTL, "reload", "nginx"], timeout_s=_ACTION_TIMEOUT_S)
    if cp.returncode != 0:
        detail = _tail(cp.stderr or cp.stdout) or f"exit status {cp.returncode}"
        raise ActionError(HELPER_INTERNAL, f"reload failed: {detail}")
    return {"ok": True}


@register("nginx.status", EmptyArgs)
def nginx_status(args: EmptyArgs) -> dict[str, Any]:
    """nginx service state: is-active / is-enabled plus the binary version string.

    ``systemctl is-active`` / ``is-enabled`` exit non-zero for inactive/disabled
    services while still printing the state on stdout, so their exit codes are
    ignored and the stdout word is used.
    """
    active = _run([_SYSTEMCTL, "is-active", "nginx"], timeout_s=_READ_TIMEOUT_S)
    enabled = _run([_SYSTEMCTL, "is-enabled", "nginx"], timeout_s=_READ_TIMEOUT_S)
    version = _run([_NGINX, "-v"], timeout_s=_READ_TIMEOUT_S)
    version_text = (version.stderr.strip() or version.stdout.strip()).replace("nginx version: ", "")
    return {
        "active": active.stdout.strip() or "unknown",
        "enabled": enabled.stdout.strip() or "unknown",
        "version": version_text,
    }


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


def _parse_openssl_date(raw: str) -> str:
    """Convert an openssl ``notAfter`` string to an ISO 8601 UTC timestamp (or "")."""
    cleaned = " ".join(raw.replace("GMT", "").split())
    try:
        dt = datetime.strptime(cleaned, _OPENSSL_DATE_FMT).replace(tzinfo=UTC)
    except ValueError:
        return ""
    return dt.isoformat()


def _inspect_cert(path: str) -> dict[str, str]:
    """Return ``{subject, expires, not_after}`` for a PEM cert via ``openssl x509``."""
    cp = _run(
        [_OPENSSL, "x509", "-noout", "-subject", "-enddate", "-in", path],
        timeout_s=_READ_TIMEOUT_S,
    )
    subject = ""
    not_after = ""
    if cp.returncode == 0:
        for line in cp.stdout.splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            if key == "subject":
                subject = value.strip()
            elif key == "notAfter":
                not_after = value.strip()
    return {"subject": subject, "expires": _parse_openssl_date(not_after), "not_after": not_after}


@register("nginx.list_certs", CertDirArgs)
def nginx_list_certs(args: CertDirArgs) -> dict[str, Any]:
    """List ``*.crt`` / ``*.pem`` certificates under ``cert_dir`` with parsed metadata.

    A missing ``cert_dir`` yields an empty list (it is created lazily on first
    write). Each entry carries the file's stem as ``name`` plus whether a
    matching ``<name>.key`` private key sits alongside it.
    """
    cert_dir = _validate_path(args.cert_dir)
    certs: list[dict[str, Any]] = []
    try:
        scan = os.scandir(cert_dir)
    except (FileNotFoundError, NotADirectoryError):
        return {"certs": []}
    except PermissionError as exc:
        raise ActionError(HELPER_INTERNAL, f"cannot scan {cert_dir}") from exc
    with scan:
        for entry in scan:
            if entry.name.startswith(".") or not entry.name.endswith((".crt", ".pem")):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            stem = entry.name.rsplit(".", 1)[0]
            info = _inspect_cert(os.path.join(cert_dir, entry.name))
            certs.append(
                {
                    "name": stem,
                    "file": entry.name,
                    "has_key": os.path.isfile(os.path.join(cert_dir, stem + ".key")),
                    **info,
                }
            )
    certs.sort(key=lambda item: str(item["file"]))
    return {"certs": certs}


@register("nginx.write_cert", WriteCertArgs)
def nginx_write_cert(args: WriteCertArgs) -> dict[str, Any]:
    """Write ``<name>.crt`` (0644) + ``<name>.key`` (0600) from uploaded PEM text."""
    cert_dir = _validate_path(args.cert_dir)
    name = _validate_name(args.name)
    if "BEGIN CERTIFICATE" not in args.cert_pem:
        raise ActionError(HELPER_INVALID_ARGS, "cert_pem is not a PEM certificate")
    if "PRIVATE KEY" not in args.key_pem:
        raise ActionError(HELPER_INVALID_ARGS, "key_pem is not a PEM private key")
    _ensure_dir(cert_dir, 0o750)
    crt = os.path.join(cert_dir, name + ".crt")
    key = os.path.join(cert_dir, name + ".key")
    _write_atomic(key, args.key_pem.encode("utf-8"), 0o600)
    try:
        _write_atomic(crt, args.cert_pem.encode("utf-8"), 0o644)
    except BaseException:
        with suppress(OSError):
            os.unlink(key)
        raise
    return {"name": name}


@register("nginx.delete_cert", CertArgs)
def nginx_delete_cert(args: CertArgs) -> dict[str, Any]:
    """Remove ``<name>.crt`` / ``<name>.pem`` / ``<name>.key`` (404 when none exist)."""
    cert_dir = _validate_path(args.cert_dir)
    name = _validate_name(args.name)
    removed = False
    for suffix in (".crt", ".pem", ".key"):
        path = os.path.join(cert_dir, name + suffix)
        if _lexists(path):
            with _map_os_errors(path):
                os.unlink(path)
            removed = True
    if not removed:
        raise ActionError(HELPER_NOT_FOUND, f"certificate not found: {name}")
    return {"name": name}


@register("nginx.generate_self_signed", SelfSignedArgs)
def nginx_generate_self_signed(args: SelfSignedArgs) -> dict[str, Any]:
    """Generate a self-signed RSA-2048 cert/key pair for ``domain`` via ``openssl req``."""
    cert_dir = _validate_path(args.cert_dir)
    name = _validate_name(args.name)
    domain = _validate_domain(args.domain)
    _ensure_dir(cert_dir, 0o750)
    crt = os.path.join(cert_dir, name + ".crt")
    key = os.path.join(cert_dir, name + ".key")
    if _lexists(crt) or _lexists(key):
        raise ActionError(HELPER_CONFLICT, f"certificate already exists: {name}")
    cp = _run(
        [
            _OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            key,
            "-out",
            crt,
            "-days",
            str(args.days),
            "-subj",
            f"/CN={domain}",
            "-addext",
            f"subjectAltName=DNS:{domain}",
        ],
        timeout_s=_OPENSSL_TIMEOUT_S,
    )
    if cp.returncode != 0:
        for path in (crt, key):
            with suppress(OSError):
                os.unlink(path)
        raise ActionError(HELPER_INTERNAL, _tail(cp.stderr) or "openssl req failed")
    with suppress(OSError):
        os.chmod(key, 0o600)
        # openssl writes the cert with the process umask; force 0644 so the mode
        # is deterministic (the cert is public, readable by the nginx worker).
        os.chmod(crt, 0o644)
    return {"name": name, **_inspect_cert(crt)}
