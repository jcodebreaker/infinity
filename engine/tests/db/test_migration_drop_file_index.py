"""The migration that drops the engine's file index tables, and rolls back to them."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "kn1b2c3d4e5f"
REVISION = "kn2b3c4d5e6f"
INDEX_TABLES = {"file_index_jobs", "file_chunks"}


def _tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_drop_file_index_migration_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'index.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert ScriptDirectory.from_config(config).get_revision(REVISION) is not None
    assert not INDEX_TABLES & _tables(engine)
    assert "artifact_publications" in _tables(engine)

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS)
    assert _tables(engine) >= INDEX_TABLES
    columns = {c["name"] for c in sa.inspect(engine).get_columns("file_chunks")}
    assert {"artifact_id", "page_start", "embedding", "embed_model"} <= columns

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert not INDEX_TABLES & _tables(engine)
