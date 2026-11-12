"""Tasks: one record per deliverable in progress in a Conversation.

Revision ID: tk1b2c3d4e5f
Revises: sg1b2c3d4e5f
Create Date: 2026-10-17 20:00:00.000000

ADR 0025: the engine keeps a deliverable's state (kind, title, the fields its kind needs, what
the person answered, status, and the artifact groups and Helpers it produced) so a pick resumes
the work instead of every turn re-deriving the task from the transcript.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "tk1b2c3d4e5f"
down_revision: str | None = "sg1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_tasks_conversation"


def upgrade() -> None:
    """Create the table and its per-Conversation index."""
    op.create_table(
        "tasks",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("fields", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("artifact_group_ids", sa.Text(), nullable=False),
        sa.Column("helper_ids", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(_INDEX, "tasks", ["workspace_id", "conversation_id", "status"])


def downgrade() -> None:
    """Drop the index and the table."""
    op.drop_index(_INDEX, table_name="tasks")
    op.drop_table("tasks")
