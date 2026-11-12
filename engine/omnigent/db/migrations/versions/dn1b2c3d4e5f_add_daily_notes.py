"""Add the daily_notes table.

Revision ID: dn1b2c3d4e5f
Revises: ts1b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000

One note per owner per local day (what we talked about, decisions, promises, open loops),
written by background passes and editable by the person.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "dn1b2c3d4e5f"
down_revision: str | None = "ts1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the daily_notes table."""
    op.create_table(
        "daily_notes",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("note_date", sa.String(10), nullable=False),
        sa.Column("sections", sa.LargeBinary(), nullable=False),
        sa.Column("edited_sections", sa.String(512), nullable=False, server_default="[]"),
        sa.Column("edited_by_person", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("parent_session_id", Uuid16(), nullable=True),
        sa.Column("quiet_passes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finalized_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "note_date"),
    )


def downgrade() -> None:
    """Drop the daily_notes table."""
    op.drop_table("daily_notes")
