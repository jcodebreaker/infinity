"""Add the objectives, objective_tasks and objective_proposals tables.

Revision ID: ob1b2c3d4e5f
Revises: sp1b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000

An objective is an outcome pursued over time under a parent session; its plan
is a list of tasks, and changes to the plan's shape go through proposals.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ob1b2c3d4e5f"
down_revision: str | None = "sp1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace_id() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Create the three objective tables."""
    op.create_table(
        "objectives",
        _workspace_id(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("parent_session_id", Uuid16(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("due", sa.String(32), nullable=True),
        sa.Column("scheduled_task_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'done', 'archived')", name="ck_objectives_status"
        ),
    )
    op.create_index(
        "ix_objectives_parent",
        "objectives",
        ["workspace_id", "parent_session_id", "created_at", "id"],
    )
    op.create_index(
        "ix_objectives_scheduled_task", "objectives", ["workspace_id", "scheduled_task_id"]
    )
    op.create_table(
        "objective_tasks",
        _workspace_id(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("objective_id", Uuid16(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("note", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'done', 'blocked', 'skipped')",
            name="ck_objective_tasks_status",
        ),
    )
    op.create_index(
        "ix_objective_tasks_objective",
        "objective_tasks",
        ["workspace_id", "objective_id", "position"],
    )
    op.create_table(
        "objective_proposals",
        _workspace_id(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("objective_id", Uuid16(), nullable=False),
        sa.Column("reason", sa.LargeBinary(), nullable=False),
        sa.Column("plan", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint(
            "status IN ('open', 'accepted', 'dismissed')", name="ck_objective_proposals_status"
        ),
    )
    op.create_index(
        "ix_objective_proposals_objective",
        "objective_proposals",
        ["workspace_id", "objective_id", "status"],
    )


def downgrade() -> None:
    """Drop the three objective tables."""
    op.drop_table("objective_proposals")
    op.drop_table("objective_tasks")
    op.drop_table("objectives")
