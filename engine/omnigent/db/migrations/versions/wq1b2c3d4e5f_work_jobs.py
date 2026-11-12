"""Work queue: one table every background model run waits in.

Revision ID: wq1b2c3d4e5f
Revises: tt1b2c3d4e5f
Create Date: 2026-10-25 12:00:00.000000

ADR 0038 (Work queue): claimed under a row lock, bound to the run it started, finished when the
run ends; idempotent on ``(kind, key)`` per workspace.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "wq1b2c3d4e5f"
down_revision: str | None = "tt1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``work_jobs`` and its indexes."""
    op.create_table(
        "work_jobs",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("owner_limit", sa.SmallInteger(), nullable=True),
        sa.Column("not_before", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.BigInteger(), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "kind", "key", name="uq_work_jobs_kind_key"),
    )
    op.create_index("ix_work_jobs_due", "work_jobs", ["state", "not_before"])
    op.create_index("ix_work_jobs_owner", "work_jobs", ["workspace_id", "owner", "state"])
    op.create_index("ix_work_jobs_run", "work_jobs", ["run_id"])


def downgrade() -> None:
    """Drop the indexes and the table."""
    op.drop_index("ix_work_jobs_run", table_name="work_jobs")
    op.drop_index("ix_work_jobs_owner", table_name="work_jobs")
    op.drop_index("ix_work_jobs_due", table_name="work_jobs")
    op.drop_table("work_jobs")
