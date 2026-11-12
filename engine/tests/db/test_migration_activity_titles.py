"""The ``activity_titles`` table exists on a freshly migrated database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_activity_titles_table_is_keyed_by_workspace_and_response(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    columns = {column["name"]: column for column in inspector.get_columns("activity_titles")}
    assert set(columns) == {
        "workspace_id",
        "response_id",
        "conversation_id",
        "title",
        "summary",
        "created_at",
    }
    assert columns["summary"]["nullable"]
    assert all(not column["nullable"] for name, column in columns.items() if name != "summary")
    assert inspector.get_pk_constraint("activity_titles")["constrained_columns"] == [
        "workspace_id",
        "response_id",
    ]
