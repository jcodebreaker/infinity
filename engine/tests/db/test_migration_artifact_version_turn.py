"""The migration that records the turn and chat that saved an artifact version."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "dc1b2c3d4e5f"
REVISION = "rf1b2c3d4e5f"


def _columns(engine: sa.engine.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("artifacts")}


def test_artifact_version_turn_migration_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'artifacts.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert REVISION in {
            r.revision for r in ScriptDirectory.from_config(config).walk_revisions()
        }
    assert {"turn_item_id", "chat_id", "origin", "source_path"} <= _columns(engine)

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS)
    assert not {"turn_item_id", "chat_id"} & _columns(engine)
    assert {"origin", "source_path"} <= _columns(engine)
