"""Add the suggestions table.

Revision ID: su1b2c3d4e5f
Revises: ob1b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000

A suggestion (an Idea) is a concrete next step an agent proposes under a parent
session, recorded by a background run and accepted or dismissed from a UI.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "su1b2c3d4e5f"
down_revision: str | None = "ob1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the suggestions table."""
    op.create_table(
        "suggestions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("parent_session_id", Uuid16(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("why", sa.LargeBinary(), nullable=False),
        sa.Column("message", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("source_session_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint(
            "status IN ('open', 'done', 'dismissed')", name="ck_suggestions_status"
        ),
    )
    op.create_index(
        "ix_suggestions_parent",
        "suggestions",
        ["workspace_id", "parent_session_id", "created_at", "id"],
    )
    op.create_index(
        "ix_suggestions_owner",
        "suggestions",
        ["workspace_id", "user_id", "status", "created_at"],
    )


def downgrade() -> None:
    """Drop the suggestions table."""
    op.drop_table("suggestions")
