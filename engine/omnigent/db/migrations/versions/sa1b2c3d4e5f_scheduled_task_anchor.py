"""Scheduled task ``anchor_at``: where an interval rule counts its periods from.

Revision ID: sa1b2c3d4e5f
Revises: kn2b3c4d5e6f
Create Date: 2026-10-10 00:00:00.000000

``scheduled_tasks.anchor_at`` is the person's start date (midnight in the task timezone), else
the moment the rule was set. It fixes the phase of ``INTERVAL>1`` rules ("every other Friday")
and holds a rule back until its start day. Existing rows keep their creation time as anchor.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sa1b2c3d4e5f"
down_revision: str | None = "kn2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column and backfill it from ``created_at``."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("anchor_at", sa.Integer(), nullable=True))
    op.execute("UPDATE scheduled_tasks SET anchor_at = created_at")


def downgrade() -> None:
    """Drop the column."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("anchor_at")
