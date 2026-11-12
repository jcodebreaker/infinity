"""Drop screen recordings, keyframes and taught skills.

Revision ID: sd1b2c3d4e5f
Revises: sa1b2c3d4e5f
Create Date: 2026-10-10 00:00:00.000000

The host no longer has a live screen or teach-a-task, so the tables that held what a person
did on it (recordings, keyframes) and the skills drafted from it (taught skills, their versions)
are dropped. The downgrade recreates them empty.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "sd1b2c3d4e5f"
down_revision: str | None = "sa1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Drop the recording and taught-skill tables."""
    op.drop_table("taught_skill_versions")
    op.drop_table("taught_skills")
    op.drop_table("recording_keyframes")
    op.drop_table("computer_recordings")


def downgrade() -> None:
    """Recreate the (empty) recording and taught-skill tables."""
    op.create_table(
        "computer_recordings",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("goal", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="recording"),
        sa.Column("actions", sa.LargeBinary(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("stopped_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint(
            "status IN ('recording', 'stopped', 'failed')",
            name="ck_computer_recordings_status",
        ),
    )
    op.create_index(
        "ix_computer_recordings_session",
        "computer_recordings",
        ["workspace_id", "session_id", "status"],
    )
    op.create_table(
        "recording_keyframes",
        _workspace(),
        sa.Column("recording_id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(16), nullable=False),
        sa.Column("jpeg", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "recording_id", "name"),
    )
    op.create_table(
        "taught_skills",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("parent_session_id", Uuid16(), nullable=False),
        sa.Column("recording_id", Uuid16(), nullable=True),
        sa.Column("name", sa.String(256), nullable=False, server_default=""),
        sa.Column("goal", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="recording"),
        sa.Column("doc", sa.LargeBinary(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.Column("stopped_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint(
            "status IN ('recording', 'drafting', 'draft', 'saved', 'failed')",
            name="ck_taught_skills_status",
        ),
    )
    op.create_index(
        "ix_taught_skills_parent",
        "taught_skills",
        ["workspace_id", "parent_session_id", "created_at", "id"],
    )
    op.create_table(
        "taught_skill_versions",
        _workspace(),
        sa.Column("skill_id", Uuid16(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("doc", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "skill_id", "version"),
    )
