"""Add jobs.params and jobs.resource_key (code-plan-m2-m5 §1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-22

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
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("params", sa.Text(), nullable=True))
        batch.add_column(sa.Column("resource_key", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("resource_key")
        batch.drop_column("params")
