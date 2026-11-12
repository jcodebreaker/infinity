"""Add approval rules, daily spending cap/spend and persisted pending approvals.

Revision ID: ap1b2c3d4e5f
Revises: pp1b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ap1b2c3d4e5f"
down_revision: str | None = "pp1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Create the four approval tables."""
    op.create_table(
        "approval_rules",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("target", sa.String(256), nullable=False),
        sa.Column("label", sa.String(256), nullable=False, server_default=""),
        sa.Column("decision", sa.String(8), nullable=False, server_default="allow"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint("decision IN ('allow', 'deny')", name="ck_approval_rules_decision"),
    )
    op.create_index(
        "ix_approval_rules_user",
        "approval_rules",
        ["workspace_id", "user_id", "created_at", "id"],
    )
    op.create_table(
        "approval_settings",
        _workspace(),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("daily_cap_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
    )
    op.create_table(
        "approval_spend",
        _workspace(),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("usd", sa.Float(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "day"),
    )
    op.create_table(
        "approval_pending",
        _workspace(),
        sa.Column("elicitation_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("target", sa.String(256), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("amount_usd", sa.Float(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "elicitation_id"),
    )
    op.create_index(
        "ix_approval_pending_user", "approval_pending", ["workspace_id", "user_id", "created_at"]
    )
    op.create_index(
        "ix_approval_pending_session", "approval_pending", ["workspace_id", "session_id"]
    )


def downgrade() -> None:
    """Drop the approval tables."""
    op.drop_index("ix_approval_pending_session", table_name="approval_pending")
    op.drop_index("ix_approval_pending_user", table_name="approval_pending")
    op.drop_table("approval_pending")
    op.drop_table("approval_spend")
    op.drop_table("approval_settings")
    op.drop_index("ix_approval_rules_user", table_name="approval_rules")
    op.drop_table("approval_rules")
