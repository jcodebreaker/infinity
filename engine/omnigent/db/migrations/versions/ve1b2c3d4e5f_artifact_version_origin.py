"""Artifact version origin, parent, source path, edit summary; unique (group, version).

Revision ID: ve1b2c3d4e5f
Revises: mc1b2c3d4e5f
Create Date: 2026-10-08 00:00:00.000000

A version can now be saved by the Muse (``ai``), edited by hand (``manual``) or restored
(``restore``); ``delivered_at`` marks a manual version already written back to the host.
The ``(workspace, session, name, version)`` uniqueness is made real so concurrent saves cannot
mint the same version number.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ve1b2c3d4e5f"
down_revision: str | None = "mc1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the columns, renumber any duplicate versions, then add the unique index."""
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("origin", sa.String(16), nullable=False, server_default="ai"))
        batch.add_column(sa.Column("parent_version_id", Uuid16(), nullable=True))
        batch.add_column(sa.Column("source_path", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("edit_summary", sa.Text(), nullable=True))
        batch.add_column(sa.Column("delivered_at", sa.Integer(), nullable=True))

    # Pre-existing duplicates (the old index was not unique): renumber by creation order.
    bind = op.get_bind()
    artifacts = sa.table(
        "artifacts",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("parent_session_id", Uuid16()),
        sa.column("name", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("created_at", sa.Integer()),
    )
    rows = bind.execute(
        sa.select(
            artifacts.c.workspace_id,
            artifacts.c.id,
            artifacts.c.parent_session_id,
            artifacts.c.name,
            artifacts.c.version,
        ).order_by(artifacts.c.created_at, artifacts.c.version)
    ).all()
    seen: dict[tuple[object, object, object], int] = {}
    for ws, row_id, session_id, name, version in rows:
        key = (ws, session_id, name)
        nxt = seen.get(key, 0) + 1
        seen[key] = nxt
        if nxt != version:
            bind.execute(
                artifacts.update()
                .where(artifacts.c.workspace_id == ws, artifacts.c.id == row_id)
                .values(version=nxt)
            )

    op.create_index(
        "uq_artifacts_session_name_version",
        "artifacts",
        ["workspace_id", "parent_session_id", "name", "version"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the unique index and the columns."""
    op.drop_index("uq_artifacts_session_name_version", table_name="artifacts")
    with op.batch_alter_table("artifacts") as batch:
        for col in ("delivered_at", "edit_summary", "source_path", "parent_version_id", "origin"):
            batch.drop_column(col)
