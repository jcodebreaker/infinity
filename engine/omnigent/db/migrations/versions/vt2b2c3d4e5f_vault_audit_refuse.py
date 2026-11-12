"""Let the vault audit record refused fills.

Revision ID: vt2b2c3d4e5f
Revises: vt1b2c3d4e5f
Create Date: 2026-10-04 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "vt2b2c3d4e5f"
down_revision: str | None = "vt1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _swap(allowed: str) -> None:
    with op.batch_alter_table("vault_audit") as batch:
        batch.drop_constraint("ck_vault_audit_action", type_="check")
        batch.create_check_constraint("ck_vault_audit_action", f"action IN ({allowed})")


def upgrade() -> None:
    """Allow the ``refuse`` action."""
    _swap("'create', 'fill', 'delete', 'refuse'")


def downgrade() -> None:
    """Restore the three-action constraint."""
    op.execute(sa.text("DELETE FROM vault_audit WHERE action = 'refuse'"))
    _swap("'create', 'fill', 'delete'")
