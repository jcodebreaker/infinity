"""Topic watch: per-source cursors, the seen-set and the run record.

Revision ID: tw1b2c3d4e5f
Revises: ah1b2c3d4e5f
Create Date: 2026-10-23 12:00:00.000000

ADR 0035 (Signal filtering): an hourly run reads what changed since its cursor per source,
judges what matched a topic once, and records counts per stage for each run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "tw1b2c3d4e5f"
down_revision: str | None = "ah1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Create the three tables and their owner indexes."""
    op.create_table(
        "topic_watch_cursors",
        _workspace(),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("cursor", sa.String(512), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "source"),
    )
    op.create_table(
        "topic_watch_seen",
        _workspace(),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("item_key", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("thread_ref", sa.String(255), nullable=True),
        sa.Column("topic_key", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("line", sa.String(512), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "item_key"),
    )
    op.create_index(
        "ix_topic_watch_seen_owner_at", "topic_watch_seen", ["workspace_id", "owner", "at"]
    )
    op.create_table(
        "topic_watch_runs",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("scheduled_task_id", Uuid16(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("counts", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_topic_watch_runs_owner_started",
        "topic_watch_runs",
        ["workspace_id", "owner", "started_at"],
    )


def downgrade() -> None:
    """Drop the indexes and the tables."""
    op.drop_index("ix_topic_watch_runs_owner_started", table_name="topic_watch_runs")
    op.drop_table("topic_watch_runs")
    op.drop_index("ix_topic_watch_seen_owner_at", table_name="topic_watch_seen")
    op.drop_table("topic_watch_seen")
    op.drop_table("topic_watch_cursors")
