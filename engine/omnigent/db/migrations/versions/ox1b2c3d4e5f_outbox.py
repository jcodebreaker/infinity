"""The outbox: proactive messages, each channel's cursor and the person's *Not useful*.

Revision ID: ox1b2c3d4e5f
Revises: tp1b2c3d4e5f
Create Date: 2026-10-23 12:00:00.000000

ADR 0036: every proactive message writes one ``outbox_items`` row; each channel reads the rows
after its own ``outbox_cursors`` row (the ``web`` channel's is the person's last seen message);
``outbox_feedback`` keeps *Not useful*, which topic watch turns into a mute.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ox1b2c3d4e5f"
down_revision: str | None = "tp1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Create the three tables and their indexes."""
    op.create_table(
        "outbox_items",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("item_id", Uuid16(), nullable=True),
        sa.Column("response_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("topic_key", sa.String(128), nullable=True),
        sa.Column("urgency", sa.String(8), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("title", sa.String(320), nullable=False, server_default=""),
        sa.Column("line", sa.String(640), nullable=False, server_default=""),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_outbox_items_owner", "outbox_items", ["workspace_id", "owner", "created_at_ms"]
    )
    op.create_index("ix_outbox_items_item", "outbox_items", ["workspace_id", "item_id"])
    op.create_index("ix_outbox_items_open", "outbox_items", ["workspace_id", "open"])
    op.create_table(
        "outbox_cursors",
        _workspace(),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("cursor", Uuid16(), nullable=False),
        sa.Column("cursor_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "channel", "owner"),
    )
    op.create_table(
        "outbox_feedback",
        _workspace(),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("item_id", Uuid16(), nullable=False),
        sa.Column("topic_key", sa.String(128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "item_id"),
    )


def downgrade() -> None:
    """Drop the tables and indexes."""
    op.drop_table("outbox_feedback")
    op.drop_table("outbox_cursors")
    op.drop_index("ix_outbox_items_open", table_name="outbox_items")
    op.drop_index("ix_outbox_items_item", table_name="outbox_items")
    op.drop_index("ix_outbox_items_owner", table_name="outbox_items")
    op.drop_table("outbox_items")
