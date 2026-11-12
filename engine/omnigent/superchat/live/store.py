"""One ``artifact_live`` row per live page: its settings and what its updates remember."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlArtifactLive, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)


def new_state() -> dict[str, Any]:
    """The state of a page nobody has updated yet.

    ``written``: ``{element id: text the last update wrote}``; ``rejected``: ``[[id, value]]``
    the person undid; ``facts``: what the sources said last time; ``last_update``:
    ``{version, applied, suggestions, at}``; ``misses``: runs in a row with unplaced changes;
    ``failures``: failed runs in a row; ``last_failure``: ``{at, reason}`` of the latest;
    ``on_change``: start an update when a source file changes in the host (a setting, kept here
    so the table stays as it is); ``note``: the person's own "what to update" text, read by
    every run; ``held``: ``{version, reason}`` of an update the published copy did not take
    (``suggestions``, ``unplaced`` or ``edits``: hand edits since the last publish), until the
    person publishes it.
    """
    return {
        "written": {},
        "facts": {},
        "rejected": [],
        "last_update": None,
        "open_issue": None,
        "misses": 0,
        "failures": 0,
        "last_failure": None,
        "checked_at": None,
        "on_change": True,
        "note": "",
        "held": None,
    }


@dataclass
class Live:
    """The live settings and state of one page."""

    parent_session_id: str
    name: str
    user_id: str | None
    task_id: str
    sources: list[str] = field(default_factory=list)
    tell: str = "notable"
    follow_publish: bool = True
    state: dict[str, Any] = field(default_factory=new_state)


def _to_entity(row: SqlArtifactLive) -> Live:
    return Live(
        parent_session_id=row.parent_session_id,
        name=row.name,
        user_id=row.user_id,
        task_id=row.task_id,
        sources=json.loads(row.sources),
        tell=row.tell,
        follow_publish=bool(row.follow_publish),
        state={**new_state(), **json.loads(row.state)},
    )


class SqlAlchemyLiveStore:
    """SQLAlchemy-backed store for the ``artifact_live`` table."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        prefix = "omnigent.artifact_live_store"
        self._session = make_named_managed_session_maker(engine, query_name_prefix=prefix)
        self._write = make_named_managed_session_maker(
            engine, query_name_prefix=prefix, immediate=True
        )

    def get(self, parent_session_id: str, name: str) -> Live | None:
        with self._session("select_artifact_live") as session:
            row = session.get(SqlArtifactLive, (current_workspace_id(), parent_session_id, name))
            return _to_entity(row) if row is not None else None

    def by_task(self, task_id: str) -> Live | None:
        with self._session("select_artifact_live_by_task") as session:
            row = session.execute(
                select(SqlArtifactLive).where(
                    SqlArtifactLive.workspace_id == current_workspace_id(),
                    SqlArtifactLive.task_id == task_id,
                )
            ).scalar_one_or_none()
            return _to_entity(row) if row is not None else None

    def of_user(self, user_id: str | None) -> list[Live]:
        """Every live page of one owner."""
        with self._session("select_artifact_live_of_user") as session:
            rows = session.execute(
                select(SqlArtifactLive).where(
                    SqlArtifactLive.workspace_id == current_workspace_id(),
                    SqlArtifactLive.user_id == user_id,
                )
            ).scalars()
            return [_to_entity(row) for row in rows]

    def save(self, live: Live) -> None:
        """Insert or replace the row."""

        def write(session: Session) -> None:
            key = (current_workspace_id(), live.parent_session_id, live.name)
            row = session.get(SqlArtifactLive, key)
            if row is None:
                row = SqlArtifactLive(parent_session_id=live.parent_session_id, name=live.name)
                session.add(row)
            row.user_id = live.user_id
            row.task_id = live.task_id
            row.sources = json.dumps(live.sources)
            row.tell = live.tell
            row.follow_publish = live.follow_publish
            row.state = json.dumps(live.state)

        run_write_transaction(self._write, "save_artifact_live", write)

    def delete(self, parent_session_id: str, name: str) -> None:
        def write(session: Session) -> None:
            session.execute(
                delete(SqlArtifactLive).where(
                    SqlArtifactLive.workspace_id == current_workspace_id(),
                    SqlArtifactLive.parent_session_id == parent_session_id,
                    SqlArtifactLive.name == name,
                )
            )

        run_write_transaction(self._write, "delete_artifact_live", write)
