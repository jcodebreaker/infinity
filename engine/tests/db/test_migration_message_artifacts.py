"""The migration that links messages to artifacts and versions to their message."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "rf1b2c3d4e5f"


def test_message_artifacts_migration_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'links.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    inspector = sa.inspect(engine)
    assert "message_artifacts" in inspector.get_table_names()
    assert "item_id" in {c["name"] for c in inspector.get_columns("artifacts")}

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS)
    inspector = sa.inspect(engine)
    assert "message_artifacts" not in inspector.get_table_names()
    assert "item_id" not in {c["name"] for c in inspector.get_columns("artifacts")}
    assert "turn_item_id" in {c["name"] for c in inspector.get_columns("artifacts")}
