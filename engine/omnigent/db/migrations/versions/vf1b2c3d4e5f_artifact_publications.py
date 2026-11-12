"""Artifact publications and view counts.

Revision ID: vf1b2c3d4e5f
Revises: ve1b2c3d4e5f
Create Date: 2026-10-08 12:00:00.000000

A deliverable (an artifact group) can be published as a small web app at ``/apps/<slug>``:
``artifact_publications`` holds one row per published group (slug, audience, pinned version) and
``artifact_views`` counts opens per slug, UTC day and opaque viewer key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "vf1b2c3d4e5f"
down_revision: str | None = "ve1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the two tables."""
    op.create_table(
        "artifact_publications",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("slug", sa.String(96), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("parent_session_id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("audience", sa.String(8), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "slug"),
    )
    op.create_index(
        "uq_artifact_publications_group",
        "artifact_publications",
        ["workspace_id", "parent_session_id", "name"],
        unique=True,
    )
    op.create_index(
        "ix_artifact_publications_owner",
        "artifact_publications",
        ["workspace_id", "user_id"],
    )
    op.create_table(
        "artifact_views",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("slug", sa.String(96), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("viewer_key", sa.String(64), nullable=False),
        sa.Column("opens", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "slug", "day", "viewer_key"),
    )


def downgrade() -> None:
    """Drop the two tables."""
    op.drop_table("artifact_views")
    op.drop_index("ix_artifact_publications_owner", table_name="artifact_publications")
    op.drop_index("uq_artifact_publications_group", table_name="artifact_publications")
    op.drop_table("artifact_publications")
