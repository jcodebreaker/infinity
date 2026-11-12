"""Editable memory, People and Dreaming: person-authored claims, commitments, nightly markers.

Revision ID: pp1b2c3d4e5f
Revises: dn1b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000

* ``memory_claims.person_authored``: the person edited the text (background writers keep out).
* ``memory_claims.kind`` also allows ``commitment``.
* ``daily_notes.dreamed_at`` / ``people_at``: the nightly passes started for the day.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pp1b2c3d4e5f"
down_revision: str | None = "dn1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    "kind IN ('preference', 'instruction', 'fact', 'decision', 'person', 'project', "
    "'working_style')"
)
_NEW = (
    "kind IN ('preference', 'instruction', 'fact', 'decision', 'person', 'project', "
    "'working_style', 'commitment')"
)


def upgrade() -> None:
    """Add the columns and widen the kind check."""
    with op.batch_alter_table("memory_claims") as batch_op:
        batch_op.add_column(
            sa.Column("person_authored", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.drop_constraint("ck_memory_claims_kind", type_="check")
        batch_op.create_check_constraint("ck_memory_claims_kind", _NEW)
    with op.batch_alter_table("daily_notes") as batch_op:
        batch_op.add_column(sa.Column("dreamed_at", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("people_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the columns and restore the kind check."""
    with op.batch_alter_table("daily_notes") as batch_op:
        batch_op.drop_column("people_at")
        batch_op.drop_column("dreamed_at")
    with op.batch_alter_table("memory_claims") as batch_op:
        batch_op.drop_constraint("ck_memory_claims_kind", type_="check")
        batch_op.create_check_constraint("ck_memory_claims_kind", _OLD)
        batch_op.drop_column("person_authored")
