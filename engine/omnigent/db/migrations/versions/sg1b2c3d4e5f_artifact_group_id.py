"""Artifact versions carry their group's stable id.

Revision ID: sg1b2c3d4e5f
Revises: ma1b2c3d4e5f
Create Date: 2026-10-17 18:00:00.000000

``artifacts.group_id`` names a deliverable (``owner``, Conversation, name) across its versions,
so a file's studio chat (ADR 0024) can point at the file and outlive a future rename. A new
group takes its first version's id; existing groups are backfilled with their version 1's id.
The session labels table is already indexed by ``(key, value)`` (``ml1b2c3d4e5f``), which the
studio's find-or-create relies on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "sg1b2c3d4e5f"
down_revision: str | None = "ma1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_artifacts_group_id"

#: Every version takes the id of its group's lowest version (version 1 unless it was deleted).
BACKFILL = (
    "UPDATE artifacts SET group_id = ("
    " SELECT v1.id FROM artifacts AS v1"
    " WHERE v1.workspace_id = artifacts.workspace_id"
    " AND v1.parent_session_id = artifacts.parent_session_id"
    " AND v1.name = artifacts.name"
    " AND ((v1.user_id IS NULL AND artifacts.user_id IS NULL)"
    " OR v1.user_id = artifacts.user_id)"
    " ORDER BY v1.version LIMIT 1)"
)


def upgrade() -> None:
    """Add the column, backfill each group with its first version's id, index it."""
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("group_id", Uuid16(), nullable=True))
    op.execute(sa.text(BACKFILL))
    op.create_index(_INDEX, "artifacts", ["workspace_id", "group_id", "version"])


def downgrade() -> None:
    """Drop the index and the column."""
    op.drop_index(_INDEX, table_name="artifacts")
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_column("group_id")
