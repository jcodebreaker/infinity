"""Add ``model_connection``: sealed model-provider API keys, per user or per organization.

Revision ID: mc1b2c3d4e5f
Revises: ml1b2c3d4e5f
Create Date: 2026-10-08 00:00:00.000000

A key is stored only as AES-GCM ciphertext; ``hint`` is its last 4 characters. ``scope`` is
``user`` (``owner_id`` is the user id) or ``org`` (``owner_id`` is ``''``). The primary key
``(workspace_id, scope, owner_id, provider)`` allows one key per provider per owner. Providers
are validated in code, so there is no provider CHECK.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mc1b2c3d4e5f"
down_revision: str | None = "ml1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table."""
    op.create_table(
        "model_connection",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("hint", sa.String(8), nullable=False),
        sa.Column("validated_at", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False, server_default="valid"),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "scope", "owner_id", "provider"),
        sa.CheckConstraint("scope IN ('org', 'user')", name="ck_model_connection_scope"),
        sa.CheckConstraint("status IN ('valid', 'invalid')", name="ck_model_connection_status"),
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_table("model_connection")
