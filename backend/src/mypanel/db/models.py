"""ORM models for the whole panel (code-plan §3).

Conventions:
- All timestamps are ``DateTime(timezone=True)`` and store UTC. Note that the
  SQLite dialect persists them without the offset, so values read back are
  naive UTC; callers must reattach ``UTC`` before comparing with aware values.
- Enum-like columns store plain strings; the ``StrEnum`` classes below define
  the allowed values but are not mapped as SQLAlchemy ``Enum`` types.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mypanel.db.base import Base


class SessionState(enum.StrEnum):
    """Values of ``Session.state`` (login state machine)."""

    MFA_PENDING = "mfa_pending"
    TOTP_ENROLL = "totp_enroll"
    ACTIVE = "active"


class JobStatus(enum.StrEnum):
    """Values of ``Job.status``."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reauth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str] = mapped_column(Text)


class MfaDevice(Base):
    __tablename__ = "mfa_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))
    secret: Mapped[str] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Highest TOTP counter accepted for this device; a code is one-time use and is
    # rejected unless its counter is strictly greater (RFC 6238 section 5.2).
    last_timecode: Mapped[int | None] = mapped_column(default=None)


class Setting(Base):
    """Key/value store (JSON-encoded values), e.g. bootstrap token state."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    request_id: Mapped[str] = mapped_column(String(32))
    actor_user_id: Mapped[int | None]
    source_ip: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str] = mapped_column(Text)
    resource: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int]


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    progress: Mapped[int | None]
    stage: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(String(32))
    log_path: Mapped[str | None] = mapped_column(Text)


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)


class TerminalSession(Base):
    __tablename__ = "terminal_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str] = mapped_column(String(64))
    exit_status: Mapped[int | None]
    duration_ms: Mapped[int | None]


class ComposeProject(Base):
    __tablename__ = "compose_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MetricSample(Base):
    """One aggregated sample; ``data`` is JSON text (cpu/load/mem/swap/disk/net)."""

    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data: Mapped[str] = mapped_column(Text)
