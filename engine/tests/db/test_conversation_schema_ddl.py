"""Dialect-specific DDL checks for split conversation databases."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from sqlalchemy import Engine, ExecutableDDLElement, create_mock_engine

from omnigent.db.db_models import ConversationBase
from omnigent.db.utils import _ensure_conversation_tables

_TITLE_INDEX = "ix_conversations_title_trgm"


def _capture_ddl(
    database_url: str,
    create_schema: Callable[[Engine], None],
) -> list[str]:
    statements: list[str] = []

    def record(statement: ExecutableDDLElement, *args: object, **kwargs: object) -> None:
        del args, kwargs
        statements.append(str(statement.compile(dialect=engine.dialect)))

    engine = create_mock_engine(database_url, record)
    create_schema(cast(Engine, engine))
    return statements


def _create_metadata(engine: Engine) -> None:
    ConversationBase.metadata.create_all(bind=engine, checkfirst=True)


def test_title_trigram_metadata_index_skipped_on_postgres() -> None:
    postgres_ddl = _capture_ddl("postgresql+psycopg://", _create_metadata)

    assert not any(_TITLE_INDEX in statement for statement in postgres_ddl)


def test_postgres_split_schema_does_not_require_pg_trgm() -> None:
    statements = _capture_ddl("postgresql+psycopg://", _ensure_conversation_tables)

    assert any("CREATE TABLE conversations" in statement for statement in statements)
    assert not any("gin_trgm_ops" in statement for statement in statements)
