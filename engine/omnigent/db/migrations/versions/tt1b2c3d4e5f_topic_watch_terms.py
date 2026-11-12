"""Topic watch: per-day counts of watched terms in the person's own items.

Revision ID: tt1b2c3d4e5f
Revises: tw1b2c3d4e5f
Create Date: 2026-10-23 18:00:00.000000

ADR 0035 (Signal filtering, amended 2026-10-23: term specificity): a term in most of the
person's own mail is common and never counts toward a match; these rows are the last 14 days
of counts that tell it apart.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "tt1b2c3d4e5f"
down_revision: str | None = "tw1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``topic_watch_terms``."""
    op.create_table(
        "topic_watch_terms",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("term", sa.String(128), nullable=False),
        sa.Column("items", sa.Integer(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "day", "term"),
    )


def downgrade() -> None:
    """Drop ``topic_watch_terms``."""
    op.drop_table("topic_watch_terms")
