"""The migration that adds the daily notes pass lease."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "sl1b2c3d4e5f"
REVISION = "dc1b2c3d4e5f"


def _columns(engine: sa.engine.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("daily_notes")}


def test_claimed_at_migration_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'notes.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert REVISION in {
            r.revision for r in ScriptDirectory.from_config(config).walk_revisions()
        }
    assert {"claimed_at", "finalized_at", "dreamed_at", "people_at"} <= _columns(engine)

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS)
    assert "claimed_at" not in _columns(engine)
    assert {"finalized_at", "dreamed_at", "people_at"} <= _columns(engine)
