"""The migration that adds the live artifacts table."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "sd1b2c3d4e5f"
REVISION = "sl1b2c3d4e5f"


def test_live_migration_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'live.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert REVISION in {
            r.revision for r in ScriptDirectory.from_config(config).walk_revisions()
        }
    assert "artifact_live" in sa.inspect(engine).get_table_names()

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS)
    assert "artifact_live" not in sa.inspect(engine).get_table_names()
