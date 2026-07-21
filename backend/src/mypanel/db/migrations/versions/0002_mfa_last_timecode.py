"""Add mfa_devices.last_timecode for one-time TOTP consumption (code-plan §5.2).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-21

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mfa_devices", sa.Column("last_timecode", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("mfa_devices", "last_timecode")
