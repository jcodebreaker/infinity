"""Card answers: the person's pick on an ask card, kept on the message that carried it.

Revision ID: ca1b2c3d4e5f
Revises: tk1b2c3d4e5f
Create Date: 2026-10-17 23:00:00.000000

ADR 0025: a card answer is a structured event (card id, option id, label) that resumes the task
its card asked for. The answer was held in process memory; it now lives with the message, so a
restart between the pick and the turn it opens loses nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ca1b2c3d4e5f"
down_revision: str | None = "tk1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table."""
    op.create_table(
        "card_answers",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("item_id", Uuid16(), nullable=False),
        sa.Column("card_id", sa.String(200), nullable=False),
        sa.Column("option_id", sa.String(200), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("task_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "session_id", "item_id"),
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_table("card_answers")
