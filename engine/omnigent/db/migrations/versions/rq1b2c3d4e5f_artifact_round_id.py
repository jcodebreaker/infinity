"""Artifact versions carry the Round that saved them.

Revision ID: rq1b2c3d4e5f
Revises: rd1b2c3d4e5f
Create Date: 2026-10-19 12:00:00.000000

ADR 0029: ``artifacts.round_id`` is the Round of the chat that saved the version (the
Conversation's own, or its Helper's), stamped at save. The transcript draws a Round's file
cards from it; the Round view still folds its files from ``round_events``. Older versions
have none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "rq1b2c3d4e5f"
down_revision: str | None = "rd1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_artifacts_round"


def upgrade() -> None:
    """Add the column and index it by Conversation and Round."""
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("round_id", Uuid16(), nullable=True))
    op.create_index(_INDEX, "artifacts", ["workspace_id", "parent_session_id", "round_id"])


def downgrade() -> None:
    """Drop the index and the column."""
    op.drop_index(_INDEX, table_name="artifacts")
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_column("round_id")
