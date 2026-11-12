"""Rhythm: per-situation tallies and per-message behaviour events.

Revision ID: rh1b2c3d4e5f
Revises: wq1b2c3d4e5f
Create Date: 2026-10-25 12:00:00.000000

ADR 0038 (Rhythm): when a person likes to hear from the assistant unasked, learned from how they
behave after its proactive messages. ``rhythm_tallies`` holds one row per owner and situation
(16 cells); ``rhythm_events`` the shown / opened / acted-on events per message. No presence
history is stored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "rh1b2c3d4e5f"
down_revision: str | None = "wq1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Create ``rhythm_tallies`` and ``rhythm_events``."""
    op.create_table(
        "rhythm_tallies",
        _workspace(),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("cell", sa.String(32), nullable=False),
        sa.Column("yes", sa.Float(), nullable=False, server_default="0"),
        sa.Column("no", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "cell"),
    )
    op.create_table(
        "rhythm_events",
        _workspace(),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("item_id", Uuid16(), nullable=False),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("at", sa.Integer(), nullable=False),
        sa.Column("cell", sa.String(32), nullable=True),
        sa.Column("topic_keys", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "item_id", "event"),
    )
    op.create_index("ix_rhythm_events_owner_at", "rhythm_events", ["workspace_id", "owner", "at"])


def downgrade() -> None:
    """Drop the tables."""
    op.drop_index("ix_rhythm_events_owner_at", table_name="rhythm_events")
    op.drop_table("rhythm_events")
    op.drop_table("rhythm_tallies")
