"""Live artifacts: a page that keeps itself up to date on a schedule.

Revision ID: sl1b2c3d4e5f
Revises: sd1b2c3d4e5f
Create Date: 2026-10-12 12:00:00.000000

``artifact_live`` holds one row per live deliverable (an artifact group): its sources, what to
tell, whether a published copy follows updates, and the state the updates remember. The schedule
itself is a scheduled task (``task_id``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "sl1b2c3d4e5f"
down_revision: str | None = "sd1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table."""
    op.create_table(
        "artifact_live",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("parent_session_id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("sources", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("tell", sa.String(8), nullable=False, server_default="notable"),
        sa.Column("follow_publish", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.Text(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("workspace_id", "parent_session_id", "name"),
    )
    op.create_index("ix_artifact_live_task", "artifact_live", ["workspace_id", "task_id"])
    op.create_index("ix_artifact_live_owner", "artifact_live", ["workspace_id", "user_id"])


def downgrade() -> None:
    """Drop the table."""
    op.drop_index("ix_artifact_live_owner", table_name="artifact_live")
    op.drop_index("ix_artifact_live_task", table_name="artifact_live")
    op.drop_table("artifact_live")
