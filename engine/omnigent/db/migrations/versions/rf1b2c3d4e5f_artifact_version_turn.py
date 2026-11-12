"""Artifact versions: the turn and chat that saved them.

Revision ID: rf1b2c3d4e5f
Revises: dc1b2c3d4e5f
Create Date: 2026-10-14 12:00:00.000000

``artifacts.turn_item_id`` is the person's message item that opened the turn that saved the
version, ``artifacts.chat_id`` the chat that saved it. Both ``NULL`` for versions saved before
(readers fall back to matching by time) and for hand edits.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "rf1b2c3d4e5f"
down_revision: str | None = "dc1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two nullable columns."""
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("turn_item_id", Uuid16(), nullable=True))
        batch_op.add_column(sa.Column("chat_id", Uuid16(), nullable=True))


def downgrade() -> None:
    """Drop the two columns."""
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_column("chat_id")
        batch_op.drop_column("turn_item_id")
