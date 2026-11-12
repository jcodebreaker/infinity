"""Add parent binding to scheduled tasks and the owner_preferences table.

Revision ID: sp1b2c3d4e5f
Revises: nn1a2b3c4d5e
Create Date: 2026-10-03 00:00:00.000000

``scheduled_tasks.parent_session_id`` / ``agent_type`` bind a task's fires to a
parent session as Helper children. ``owner_preferences`` stores the per-owner
proactivity level and quiet hours that gate those fires. ``attempt`` on
``scheduled_task_runs`` marks the single retry of a failed fire.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "sp1b2c3d4e5f"
down_revision: str | None = "nn1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the binding columns and create owner_preferences."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("parent_session_id", Uuid16(), nullable=True))
        batch_op.add_column(sa.Column("agent_type", sa.String(128), nullable=True))
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.add_column(
            sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="1")
        )
    op.create_table(
        "owner_preferences",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("proactivity", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("quiet_start", sa.String(5), nullable=True),
        sa.Column("quiet_end", sa.String(5), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
        sa.CheckConstraint(
            "proactivity IN ('off', 'low', 'normal')", name="ck_owner_preferences_proactivity"
        ),
    )


def downgrade() -> None:
    """Drop owner_preferences and the binding columns."""
    op.drop_table("owner_preferences")
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.drop_column("attempt")
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("agent_type")
        batch_op.drop_column("parent_session_id")
