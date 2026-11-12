"""Daily notes: a lease for the nightly passes.

Revision ID: dc1b2c3d4e5f
Revises: sl1b2c3d4e5f
Create Date: 2026-10-12 18:00:00.000000

``daily_notes.claimed_at`` is the single-flight lease of a finalize, Dreaming or People pass
(epoch seconds, ``NULL`` when none runs). ``finalized_at`` / ``dreamed_at`` / ``people_at`` now
mean the pass succeeded, and are set only then.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dc1b2c3d4e5f"
down_revision: str | None = "sl1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable lease column."""
    with op.batch_alter_table("daily_notes") as batch_op:
        batch_op.add_column(sa.Column("claimed_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the lease column."""
    with op.batch_alter_table("daily_notes") as batch_op:
        batch_op.drop_column("claimed_at")
