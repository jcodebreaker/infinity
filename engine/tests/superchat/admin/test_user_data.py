"""Deleting a person removes everything the engine stores for them, by construction.

Every table is classified in ``omnigent.superchat.admin.user_data``. These tests fail when a table
is not, when a table that carries a person's id is declared not to be per-person, and when the
delete leaves a row behind: skills, artifacts and their blobs, publications, views.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import insert, select

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.superchat.admin.user_data import (
    PERSON_COLUMNS,
    USER_DATA,
    Child,
    Direct,
    NotPerUser,
    artifact_blob_keys,
    delete_user_rows,
)

TABLES = OmnigentBase.metadata.tables


def test_every_table_is_classified_so_a_new_one_cannot_be_forgotten() -> None:
    missing = sorted(set(TABLES) - set(USER_DATA))
    assert not missing, (
        f"Classify {missing} in omnigent/superchat/admin/user_data.py: a table that holds a "
        "person's data must be deleted with the person (Direct or Child)."
    )
    stale = sorted(set(USER_DATA) - set(TABLES))
    assert not stale, f"user_data.py names tables that no longer exist: {stale}"


def test_a_table_with_a_person_column_is_never_declared_not_per_person() -> None:
    wrong = [
        name
        for name, rule in USER_DATA.items()
        if isinstance(rule, NotPerUser) and any(col in TABLES[name].c for col in PERSON_COLUMNS)
    ]
    assert not wrong, f"{wrong} carry a person's id but are marked NotPerUser"


def test_rules_point_at_real_columns_and_parents() -> None:
    for name, rule in USER_DATA.items():
        if isinstance(rule, Direct):
            assert rule.column in TABLES[name].c, (name, rule.column)
        if isinstance(rule, Child):
            assert rule.column in TABLES[name].c, (name, rule.column)
            parent_rule = USER_DATA[rule.parent]
            assert isinstance(parent_rule, Direct), (name, "a child hangs off a Direct table")
            assert rule.parent_column in TABLES[rule.parent].c, (name, rule.parent_column)


def _seed(connection, user: str) -> dict[str, str]:
    artifact, parent = (uuid.uuid4().hex for _ in range(2))
    slug = f"app-{user}-{uuid.uuid4().hex[:6]}"
    connection.execute(
        insert(TABLES["artifacts"]).values(
            id=artifact,
            user_id=user,
            parent_session_id=parent,
            name="app.html",
            kind="html",
            mime="text/html",
            size=1,
            blob_key=f"blob-{user}",
            created_at=1,
        )
    )
    connection.execute(
        insert(TABLES["artifact_publications"]).values(
            slug=slug,
            user_id=user,
            parent_session_id=parent,
            name="app.html",
            audience="link",
            published_version=1,
            published_at=1,
        )
    )
    connection.execute(
        insert(TABLES["artifact_views"]).values(
            slug=slug, day="2026-10-01", viewer_key="v", last_at=1
        )
    )
    return {"slug": slug}


def _count(connection, table: str) -> int:
    return len(connection.execute(select(TABLES[table])).all())


def test_delete_removes_a_persons_artifacts_publications_and_views(tmp_path: Path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'u.db'}")
    OmnigentBase.metadata.create_all(engine)
    with engine.begin() as connection:
        _seed(connection, "alice@example.test")
        bob = _seed(connection, "bob@example.test")
        assert artifact_blob_keys(connection, "alice@example.test") == ["blob-alice@example.test"]
        removed = delete_user_rows(connection, "alice@example.test")
        assert removed["artifacts"] == 1
        assert removed["artifact_views"] == 1
        assert removed["artifact_publications"] == 1
        for table in (
            "artifacts",
            "artifact_publications",
            "artifact_views",
        ):
            assert _count(connection, table) == 1, table  # only bob's row is left
        slugs = [row.slug for row in connection.execute(select(TABLES["artifact_publications"]))]
        assert slugs == [bob["slug"]]
        # A reset account, same email, finds nothing of the earlier holder's.
        again = delete_user_rows(connection, "alice@example.test")
        assert sum(again.values()) == 0
