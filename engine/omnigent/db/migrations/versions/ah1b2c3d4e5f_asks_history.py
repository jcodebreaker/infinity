"""Decided approvals keep their row; a replaced plan proposal reads ``superseded``.

Revision ID: ah1b2c3d4e5f
Revises: ox1b2c3d4e5f
Create Date: 2026-10-23 18:00:00.000000

ADR 0036 (amended): the Inbox's History reads answered asks from their own stores. An answered
approval was deleted; it now keeps its row with ``status`` (``pending``, ``approved``,
``denied``, ``expired``), ``decided_by`` and ``decided_at``. A proposal replaced by a newer one
was ``dismissed`` like one the person dismissed; it is now ``superseded``. Rows from before stay
as they are: earlier answered approvals are gone, earlier ``dismissed`` proposals are not split.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ah1b2c3d4e5f"
down_revision: str | None = "ox1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROPOSAL_CK = "ck_objective_proposals_status"


def _proposal_statuses(allowed: str) -> None:
    with op.batch_alter_table("objective_proposals") as batch:
        batch.drop_constraint(_PROPOSAL_CK, type_="check")
        batch.create_check_constraint(_PROPOSAL_CK, f"status IN ({allowed})")


def upgrade() -> None:
    """Add the decision columns and the ``superseded`` status."""
    with op.batch_alter_table("approval_pending") as batch:
        batch.add_column(
            sa.Column("status", sa.String(16), nullable=False, server_default="pending")
        )
        batch.add_column(sa.Column("decided_by", sa.String(128), nullable=True))
        batch.add_column(sa.Column("decided_at", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_approval_pending_status",
            "status IN ('pending', 'approved', 'denied', 'expired')",
        )
    op.create_index(
        "ix_approval_pending_decided",
        "approval_pending",
        ["workspace_id", "user_id", "decided_at"],
    )
    _proposal_statuses("'open', 'accepted', 'dismissed', 'superseded'")


def downgrade() -> None:
    """Drop decided approvals and the columns; ``superseded`` reads ``dismissed`` again."""
    op.execute(
        sa.text("UPDATE objective_proposals SET status = 'dismissed' WHERE status = 'superseded'")
    )
    _proposal_statuses("'open', 'accepted', 'dismissed'")
    op.drop_index("ix_approval_pending_decided", table_name="approval_pending")
    op.execute(sa.text("DELETE FROM approval_pending WHERE status <> 'pending'"))
    with op.batch_alter_table("approval_pending") as batch:
        batch.drop_constraint("ck_approval_pending_status", type_="check")
        batch.drop_column("decided_at")
        batch.drop_column("decided_by")
        batch.drop_column("status")
