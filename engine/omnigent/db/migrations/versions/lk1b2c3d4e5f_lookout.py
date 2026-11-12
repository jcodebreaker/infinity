"""The Lookout replaces topic watch: signals, one next wake per person, the wake ledger.

Revision ID: lk1b2c3d4e5f
Revises: rh1b2c3d4e5f
Create Date: 2026-10-25 12:00:00.000000

ADR 0038: topic watch's hourly hidden tasks are deleted (with their run rows) in the same change
that unregisters the kind, so no orphan row reaches the fire path. Its cursors, seen-set and run
record go; its term counts are kept as ``lookout_terms``. ``lookout_signals`` holds what sources
push, ``lookout_owners`` each person's one next wake, ``lookout_wakes`` every wake that reached
a run (the ledger the limits count).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "lk1b2c3d4e5f"
down_revision: str | None = "rh1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Delete topic watch's tasks and tables; create the Lookout's."""
    op.execute(
        "DELETE FROM scheduled_task_runs WHERE scheduled_task_id IN "
        "(SELECT id FROM scheduled_tasks WHERE kind = 'topic_watch')"
    )
    op.execute("DELETE FROM scheduled_tasks WHERE kind = 'topic_watch'")
    op.drop_index("ix_topic_watch_runs_owner_started", table_name="topic_watch_runs")
    op.drop_table("topic_watch_runs")
    op.drop_index("ix_topic_watch_seen_owner_at", table_name="topic_watch_seen")
    op.drop_table("topic_watch_seen")
    op.drop_table("topic_watch_cursors")
    op.rename_table("topic_watch_terms", "lookout_terms")

    op.create_table(
        "lookout_signals",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("topic_key", sa.String(128), nullable=True),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=True),
        sa.Column("due_at", sa.Integer(), nullable=False),
        sa.Column("stale_after", sa.Integer(), nullable=False),
        sa.Column("urgency", sa.String(8), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("claimed_run_id", Uuid16(), nullable=True),
        sa.Column("sent_at", sa.Integer(), nullable=True),
        sa.Column("done_at", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_lookout_signals_owner_key", "lookout_signals", ["workspace_id", "owner", "dedupe_key"]
    )
    op.create_index(
        "ix_lookout_signals_owner_due",
        "lookout_signals",
        ["workspace_id", "owner", "done_at", "due_at"],
    )
    op.create_index(
        "ix_lookout_signals_claim", "lookout_signals", ["workspace_id", "claimed_run_id"]
    )
    op.create_table(
        "lookout_owners",
        _workspace(),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=True),
        sa.Column("next_wake_at", sa.Integer(), nullable=True),
        sa.Column("lease_until", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner"),
    )
    op.create_index("ix_lookout_owners_next", "lookout_owners", ["next_wake_at"])
    op.create_table(
        "lookout_wakes",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("scheduled_task_id", Uuid16(), nullable=True),
        sa.Column("conversation_id", Uuid16(), nullable=True),
        sa.Column("run_conversation_id", Uuid16(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("urgency", sa.String(8), nullable=False),
        sa.Column("state", sa.String(16), nullable=True),
        sa.Column("topic_keys", sa.Text(), nullable=False),
        sa.Column("item_id", Uuid16(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_lookout_wakes_owner_started", "lookout_wakes", ["workspace_id", "owner", "started_at"]
    )


def downgrade() -> None:
    """Drop the Lookout's tables and bring back topic watch's (empty; its tasks are not)."""
    op.drop_index("ix_lookout_wakes_owner_started", table_name="lookout_wakes")
    op.drop_table("lookout_wakes")
    op.drop_index("ix_lookout_owners_next", table_name="lookout_owners")
    op.drop_table("lookout_owners")
    op.drop_index("ix_lookout_signals_claim", table_name="lookout_signals")
    op.drop_index("ix_lookout_signals_owner_due", table_name="lookout_signals")
    op.drop_index("ix_lookout_signals_owner_key", table_name="lookout_signals")
    op.drop_table("lookout_signals")
    op.rename_table("lookout_terms", "topic_watch_terms")
    op.create_table(
        "topic_watch_cursors",
        _workspace(),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("cursor", sa.String(512), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "source"),
    )
    op.create_table(
        "topic_watch_seen",
        _workspace(),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("item_key", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("thread_ref", sa.String(255), nullable=True),
        sa.Column("topic_key", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("line", sa.String(512), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner", "item_key"),
    )
    op.create_index(
        "ix_topic_watch_seen_owner_at", "topic_watch_seen", ["workspace_id", "owner", "at"]
    )
    op.create_table(
        "topic_watch_runs",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("scheduled_task_id", Uuid16(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("counts", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_topic_watch_runs_owner_started",
        "topic_watch_runs",
        ["workspace_id", "owner", "started_at"],
    )
