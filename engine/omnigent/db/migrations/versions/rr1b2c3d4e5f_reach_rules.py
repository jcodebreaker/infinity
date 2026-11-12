"""Reach rules: how the person said to be reached.

Revision ID: rr1b2c3d4e5f
Revises: lk1b2c3d4e5f
Create Date: 2026-10-25 18:00:00.000000

ADR 0038 (amended, "what the person says is a rule"): ``reach_rules`` holds the person's own
rules on unasked messages (pause, level, hours, only for, mute), each with the sentence they said
and an optional end. The rhythm reads them before anything it learned; Start over never clears
them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "rr1b2c3d4e5f"
down_revision: str | None = "lk1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``reach_rules``."""
    op.create_table(
        "reach_rules",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("said", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index("ix_reach_rules_owner", "reach_rules", ["workspace_id", "owner"])


def downgrade() -> None:
    """Drop the table."""
    op.drop_index("ix_reach_rules_owner", table_name="reach_rules")
    op.drop_table("reach_rules")
