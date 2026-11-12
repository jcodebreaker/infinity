"""The migration that drops the screen recording and taught-skill tables."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "sa1b2c3d4e5f"
REVISION = "sd1b2c3d4e5f"
TABLES = {"computer_recordings", "recording_keyframes", "taught_skills", "taught_skill_versions"}


def _tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_drop_migration_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'drop.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        script = ScriptDirectory.from_config(config)
        assert REVISION in {rev.revision for rev in script.walk_revisions()}
    assert not TABLES & _tables(engine)

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS)
    assert _tables(engine) >= TABLES
