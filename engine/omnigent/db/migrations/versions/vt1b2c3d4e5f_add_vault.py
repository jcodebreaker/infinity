"""Add the secrets vault: saved logins, secure-entry requests and the audit trail.

Revision ID: vt1b2c3d4e5f
Revises: ap1b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000

Passwords are stored only as AES-GCM ciphertext; the audit table records who used which secret
and when, never a value.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "vt1b2c3d4e5f"
down_revision: str | None = "ap1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    """Create the vault tables."""
    op.create_table(
        "vault_secrets",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("site", sa.String(512), nullable=False),
        sa.Column("username", sa.String(256), nullable=False, server_default=""),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index("ix_vault_secrets_owner", "vault_secrets", ["workspace_id", "user_id", "name"])
    op.create_table(
        "vault_requests",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("site", sa.String(512), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False, server_default=""),
        sa.Column("status", sa.String(8), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint("status IN ('pending', 'saved')", name="ck_vault_requests_status"),
    )
    op.create_table(
        "vault_audit",
        _workspace(),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("secret_id", Uuid16(), nullable=False),
        sa.Column("secret_name", sa.String(128), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=True),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint("action IN ('create', 'fill', 'delete')", name="ck_vault_audit_action"),
    )
    op.create_index("ix_vault_audit_secret", "vault_audit", ["workspace_id", "secret_id", "at"])


def downgrade() -> None:
    """Drop the vault tables."""
    op.drop_index("ix_vault_audit_secret", table_name="vault_audit")
    op.drop_table("vault_audit")
    op.drop_table("vault_requests")
    op.drop_index("ix_vault_secrets_owner", table_name="vault_secrets")
    op.drop_table("vault_secrets")
