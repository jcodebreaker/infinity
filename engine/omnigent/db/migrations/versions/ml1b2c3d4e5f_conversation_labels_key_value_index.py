"""Index ``conversation_labels`` by ``(key, value)``.

Revision ID: ml1b2c3d4e5f
Revises: pf1b2c3d4e5f
Create Date: 2026-10-07 00:00:00.000000

Finds the sessions carrying one label value without scanning every label row, e.g. a caller's
Super Chat by its ``omnigent.superchat.muse.key`` label.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "ml1b2c3d4e5f"
down_revision: str | None = "pf1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_conversation_labels_key_value"


def upgrade() -> None:
    """Add the lookup index."""
    op.create_index(_INDEX, "conversation_labels", ["workspace_id", "key", "value"])


def downgrade() -> None:
    """Drop the lookup index."""
    op.drop_index(_INDEX, table_name="conversation_labels")
