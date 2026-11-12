"""Add the topics table.

Revision ID: tp1b2c3d4e5f
Revises: rq1b2c3d4e5f
Create Date: 2026-10-23 00:00:00.000000

ADR 0035: one row per owner per topic, upserted by key by the quiet-moment pass. Home's topic
cards, the Topics panel and the pick-up card read it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "tp1b2c3d4e5f"
down_revision: str | None = "rq1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the topics table."""
    op.create_table(
        "topics",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("last_touched_at", sa.Integer(), nullable=False),
        sa.Column("since_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "key"),
    )
    op.create_index(
        "ix_topics_owner_touched", "topics", ["workspace_id", "owner", "last_touched_at"]
    )


def downgrade() -> None:
    """Drop the topics table."""
    op.drop_index("ix_topics_owner_touched", table_name="topics")
    op.drop_table("topics")
