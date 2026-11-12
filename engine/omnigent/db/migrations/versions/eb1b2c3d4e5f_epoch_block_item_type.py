"""Admit the ``epoch_block`` item type (code 12) in ``conversation_items``.

Revision ID: eb1b2c3d4e5f
Revises: rr1b2c3d4e5f
Create Date: 2026-11-11 00:00:00.000000

An ``epoch_block`` item stores the rendered block an epoch's window starts with
(``omnigent.db.enum_codecs.ITEM_TYPE``). Only the type ``CHECK`` widens.

SQLite rebuilds the table to swap a ``CHECK``, and its reflection cannot carry
the ``position DESC`` expression index across, so that index is dropped before
the rebuild and created again after it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "eb1b2c3d4e5f"
down_revision: str | None = "rr1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversation_items"
_CHECK = "ck_conversation_items_type"
_TYPE_INDEX = "ix_conversation_items_conv_type_position"


def _swap(codes: str) -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        op.drop_index(_TYPE_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE, recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, f"type IN ({codes})")
    if sqlite:
        op.create_index(
            _TYPE_INDEX,
            _TABLE,
            ["workspace_id", "conversation_id", "type", sa.text("position DESC")],
        )


def upgrade() -> None:
    """Allow type code 12 (``epoch_block``)."""
    _swap("1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12")


def downgrade() -> None:
    """Remove ``epoch_block`` items and restore the eleven-type constraint."""
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE type = 12"))
    _swap("1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11")
