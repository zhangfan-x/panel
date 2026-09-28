"""Audit package: the write-side ``record_audit`` and the read-side query router.

``record_audit`` stays importable as ``from ownpanel.audit import record_audit``
for every existing caller; ``audit_router`` exposes the audit-log query API.
"""

from __future__ import annotations

# Order matters: bind record_audit BEFORE importing the router. The router
# pulls in ownpanel.auth.deps, whose package init imports ownpanel.auth.router,
# which itself does ``from ownpanel.audit import record_audit`` — that lookup
# only succeeds on this partially initialized package if service came first.
# isort: off
from ownpanel.audit.service import record_audit
from ownpanel.audit.router import audit_router

# isort: on

__all__ = ["audit_router", "record_audit"]
