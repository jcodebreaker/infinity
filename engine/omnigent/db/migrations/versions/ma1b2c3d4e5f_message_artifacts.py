"""Messages link to artifacts, and versions to the message that led to them.

Revision ID: ma1b2c3d4e5f
Revises: rf1b2c3d4e5f
Create Date: 2026-10-17 12:00:00.000000

``message_artifacts`` holds one row per person's message that is linked to an artifact (open
beside the chat when sent, filled in by a save during its turn, or picked on the chip).
``artifacts.item_id`` is the person's message in the Conversation a version came from: the turn
item of the Conversation's own save, or the message a Helper was started from for a Helper's.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ma1b2c3d4e5f"
down_revision: str | None = "rf1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the link table and add the version's message column."""
    op.create_table(
        "message_artifacts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("item_id", Uuid16(), nullable=False),
        sa.Column("artifact_id", Uuid16(), nullable=True),
        sa.Column("name", sa.String(512), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("how", sa.String(8), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "session_id", "item_id"),
    )
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("item_id", Uuid16(), nullable=True))


def downgrade() -> None:
    """Drop the column and the table."""
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_column("item_id")
    op.drop_table("message_artifacts")
