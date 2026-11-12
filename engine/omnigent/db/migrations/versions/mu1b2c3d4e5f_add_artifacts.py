"""Add the artifacts table.

Revision ID: mu1b2c3d4e5f
Revises: vt2b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000

An artifact version is a deliverable file (HTML page, document, sheet, deck...) the Muse saved
from its host into a Conversation; the bytes live in the artifact store.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "mu1b2c3d4e5f"
down_revision: str | None = "vt2b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the artifacts table."""
    op.create_table(
        "artifacts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("parent_session_id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("blob_key", sa.String(128), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_artifacts_group",
        "artifacts",
        ["workspace_id", "user_id", "parent_session_id", "name", "version"],
    )
    op.create_index("ix_artifacts_owner", "artifacts", ["workspace_id", "user_id", "created_at"])


def downgrade() -> None:
    """Drop the artifacts table."""
    op.drop_table("artifacts")
