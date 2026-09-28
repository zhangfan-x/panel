"""Aggregated /api/v1 router: every versioned API route is included here."""

from __future__ import annotations

from fastapi import APIRouter

from mypanel.audit import audit_router
from mypanel.auth.router import auth_router, bootstrap_router
from mypanel.compose.router import compose_router
from mypanel.dockermgr.router import docker_router
from mypanel.events import events_router
from mypanel.files.router import files_router
from mypanel.jobs.router import jobs_router
from mypanel.processes.router import processes_router
from mypanel.proxy.router import proxy_router
from mypanel.services.router import services_router
from mypanel.settings.router import settings_router
from mypanel.system.router import router as system_router
from mypanel.terminal.router import terminal_router
from mypanel.website.firewall import firewall_router
from mypanel.website.router import website_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(bootstrap_router)
api_router.include_router(auth_router)
api_router.include_router(system_router)
api_router.include_router(events_router)
api_router.include_router(jobs_router)
api_router.include_router(processes_router)
api_router.include_router(services_router)
api_router.include_router(files_router)
api_router.include_router(docker_router)
api_router.include_router(compose_router)
api_router.include_router(terminal_router)
api_router.include_router(audit_router)
api_router.include_router(settings_router)
api_router.include_router(website_router)
api_router.include_router(firewall_router)
api_router.include_router(proxy_router)
