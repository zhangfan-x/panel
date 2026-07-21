"""Aggregated /api/v1 router: every versioned API route is included here."""

from __future__ import annotations

from fastapi import APIRouter

from mypanel.auth.router import auth_router, bootstrap_router
from mypanel.system.router import router as system_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(bootstrap_router)
api_router.include_router(auth_router)
api_router.include_router(system_router)
