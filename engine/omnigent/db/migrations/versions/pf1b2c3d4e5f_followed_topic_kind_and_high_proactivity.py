"""Scheduled task ``kind`` marker; allow proactivity ``high``.

Revision ID: pf1b2c3d4e5f
Revises: mu1b2c3d4e5f
Create Date: 2026-10-07 00:00:00.000000

``scheduled_tasks.kind`` is an explicit product marker set at creation (``followed_topic``
for a topic the Muse follows for the person), replacing a guess from the Sub-agent Type.
``owner_preferences.proactivity`` also accepts ``high``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pf1b2c3d4e5f"
down_revision: str | None = "mu1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_owner_preferences_proactivity"


def upgrade() -> None:
    """Add the kind column and widen the proactivity check."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(64), nullable=True))
    with op.batch_alter_table("owner_preferences") as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, "proactivity IN ('off', 'low', 'normal', 'high')")


def downgrade() -> None:
    """Drop the kind column; ``high`` rows fall back to ``normal``."""
    op.execute("UPDATE owner_preferences SET proactivity = 'normal' WHERE proactivity = 'high'")
    with op.batch_alter_table("owner_preferences") as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, "proactivity IN ('off', 'low', 'normal')")
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("kind")
