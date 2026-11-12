"""The migration that adds ``scheduled_tasks.anchor_at`` and backfills it from ``created_at``."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "kn2b3c4d5e6f"


def _columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")}


def test_anchor_migration_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'anchor.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert "anchor_at" in _columns(engine)

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS)
    assert "anchor_at" not in _columns(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO scheduled_tasks (id, workspace_id, name, prompt, rrule, agent_id,"
                " timezone, state, execution_target, created_at)"
                " VALUES ('t1', 0, 'n', 'p', 'FREQ=DAILY', 'a', 'UTC', 1, 1, 1234)"
            )
        )

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT anchor_at FROM scheduled_tasks")).scalar() == 1234
        )
