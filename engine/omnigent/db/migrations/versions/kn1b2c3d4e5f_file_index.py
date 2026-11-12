"""File index jobs and chunks (the knowledge capability).

Revision ID: kn1b2c3d4e5f
Revises: vf1b2c3d4e5f
Create Date: 2026-10-09 12:00:00.000000

``file_index_jobs`` holds one row per artifact version the knowledge worker indexes (state,
attempts, sha256 for de-duplication); ``file_chunks`` holds the passages (page range, heading
path, text, embedding bytes) the per-user txtai index is rebuilt from.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "kn1b2c3d4e5f"
down_revision: str | None = "vf1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the two tables."""
    op.create_table(
        "file_index_jobs",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("artifact_id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embed_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("embed_reason", sa.String(32), nullable=True),
        sa.Column("duplicate_of", Uuid16(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("workspace_id", "artifact_id"),
    )
    op.create_index(
        "ix_file_index_jobs_state",
        "file_index_jobs",
        ["workspace_id", "state", "next_attempt_at"],
    )
    op.create_index(
        "ix_file_index_jobs_owner_sha",
        "file_index_jobs",
        ["workspace_id", "user_id", "sha256"],
    )
    op.create_table(
        "file_chunks",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("artifact_id", Uuid16(), nullable=False),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("embed_model", sa.String(160), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_file_chunks_artifact", "file_chunks", ["workspace_id", "artifact_id", "ord"]
    )
    op.create_index("ix_file_chunks_owner", "file_chunks", ["workspace_id", "user_id"])


def downgrade() -> None:
    """Drop the two tables."""
    op.drop_index("ix_file_chunks_owner", table_name="file_chunks")
    op.drop_index("ix_file_chunks_artifact", table_name="file_chunks")
    op.drop_table("file_chunks")
    op.drop_index("ix_file_index_jobs_owner_sha", table_name="file_index_jobs")
    op.drop_index("ix_file_index_jobs_state", table_name="file_index_jobs")
    op.drop_table("file_index_jobs")
