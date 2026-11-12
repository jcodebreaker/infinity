"""Add the model-written result summary to activity_titles.

Revision ID: as1b2c3d4e5f
Revises: at1b2c3d4e5f
Create Date: 2026-10-04 12:00:00.000000

The Activity Feed row's second line: a short model-written summary of what a finished
turn achieved, stored beside the turn's title. Additive and nullable; existing turns
are summarised lazily on read.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "as1b2c3d4e5f"
down_revision: str | None = "at1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable summary column."""
    with op.batch_alter_table("activity_titles") as batch:
        batch.add_column(sa.Column("summary", sa.String(256), nullable=True))


def downgrade() -> None:
    """Drop the summary column."""
    with op.batch_alter_table("activity_titles") as batch:
        batch.drop_column("summary")
