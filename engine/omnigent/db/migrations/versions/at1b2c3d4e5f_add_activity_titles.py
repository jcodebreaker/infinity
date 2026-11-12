"""Add the activity_titles table.

Revision ID: at1b2c3d4e5f
Revises: su1b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000

Holds the model-written title of one chat turn in the Activity Feed, keyed by the
turn's response id. Activities are derived on read; only this title is stored.
Additive. No existing data needs backfill (turns are titled lazily on read).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "at1b2c3d4e5f"
down_revision: str | None = "su1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the activity_titles table."""
    op.create_table(
        "activity_titles",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("response_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "response_id"),
    )


def downgrade() -> None:
    """Drop the activity_titles table."""
    op.drop_table("activity_titles")
