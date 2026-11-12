"""Round events: what happened in a Round, one fact per row.

Revision ID: rd1b2c3d4e5f
Revises: ca1b2c3d4e5f
Create Date: 2026-10-18 23:00:00.000000

ADR 0029: a Round (one message from the person through its settlement) is recorded as it
happens, and its view is a pure fold over these rows. Nothing reads them yet.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "rd1b2c3d4e5f"
down_revision: str | None = "ca1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table and its Conversation index."""
    op.create_table(
        "round_events",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("round_id", Uuid16(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "round_id", "seq"),
    )
    op.create_index(
        "ix_round_events_conversation",
        "round_events",
        ["workspace_id", "conversation_id", "round_id", "seq"],
    )


def downgrade() -> None:
    """Drop the index and the table."""
    op.drop_index("ix_round_events_conversation", table_name="round_events")
    op.drop_table("round_events")
