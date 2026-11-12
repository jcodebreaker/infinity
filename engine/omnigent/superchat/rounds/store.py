"""The ``round_events`` table: a Round's events, appended in order, never changed."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlRoundEvent, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.superchat.rounds.fold import ROUND_OPENED, WAKE

#: Attempts at an append whose sequence numbers another writer took first.
_APPEND_ATTEMPTS = 3


@dataclass(frozen=True)
class RoundEvent:
    """One fact of a Round, before it is stored.

    :param round_id: The Round, e.g. the id of the person's message that opened it.
    :param conversation_id: The Conversation the Round is in.
    :param kind: e.g. ``"tool.started"``.
    :param actor: ``person``, ``system``, ``assistant`` or ``helper``.
    :param at_ms: Epoch milliseconds when the engine saw it.
    :param body: The kind's fields, e.g. ``{"call_id": "toolu_1", "name": "artifact_save"}``.
    """

    round_id: str
    conversation_id: str
    kind: str
    actor: str
    at_ms: int
    body: dict[str, Any] = field(default_factory=dict)


def _flat(row: SqlRoundEvent) -> dict[str, Any]:
    """A stored event, flat as the fold reads it (``t`` in seconds)."""
    return {
        **json.loads(row.body),
        "round_id": row.round_id,
        "seq": row.seq,
        "t": row.at_ms / 1000,
        "kind": row.kind,
        "actor": row.actor,
    }


class SqlAlchemyRoundStore:
    """SQLAlchemy-backed store for the ``round_events`` table."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.round_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.round_store", immediate=True
        )

    def append(self, events: Sequence[RoundEvent]) -> None:
        """Store *events* in order, each after the last stored event of its Round."""

        def write(session: Session) -> None:
            next_seq: dict[str, int] = {}
            for event in events:
                if event.round_id not in next_seq:
                    last = session.execute(
                        select(func.max(SqlRoundEvent.seq)).where(
                            SqlRoundEvent.workspace_id == current_workspace_id(),
                            SqlRoundEvent.round_id == event.round_id,
                        )
                    ).scalar()
                    next_seq[event.round_id] = (last or 0) + 1
                session.add(
                    SqlRoundEvent(
                        round_id=event.round_id,
                        seq=next_seq[event.round_id],
                        conversation_id=event.conversation_id,
                        kind=event.kind,
                        actor=event.actor,
                        body=json.dumps(event.body, ensure_ascii=False),
                        at_ms=event.at_ms,
                    )
                )
                next_seq[event.round_id] += 1

        for attempt in range(_APPEND_ATTEMPTS):
            try:
                run_write_transaction(self._session_immediate, "round_events_append", write)
                return
            except IntegrityError:  # another replica took the same sequence numbers
                if attempt == _APPEND_ATTEMPTS - 1:
                    raise

    def events(self, round_id: str, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """The Round's events in order, flat as the fold reads them (``t`` in seconds).

        :param conversation_id: Only a Round of this Conversation; another's reads as empty.
        """
        query = select(SqlRoundEvent).where(
            SqlRoundEvent.workspace_id == current_workspace_id(),
            SqlRoundEvent.round_id == round_id,
        )
        if conversation_id is not None:
            query = query.where(SqlRoundEvent.conversation_id == conversation_id)
        with self._session("round_events_list") as session:
            rows = session.execute(query.order_by(SqlRoundEvent.seq)).scalars()
            return [_flat(row) for row in rows]

    def events_of(
        self, conversation_id: str, round_ids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """:meth:`events` of each of the Conversation's *round_ids* in one read; a Round with no
        event is left out."""
        if not round_ids:
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        with self._session("round_events_list_many") as session:
            rows = session.execute(
                select(SqlRoundEvent)
                .where(
                    SqlRoundEvent.workspace_id == current_workspace_id(),
                    SqlRoundEvent.conversation_id == conversation_id,
                    SqlRoundEvent.round_id.in_(list(dict.fromkeys(round_ids))),
                )
                .order_by(SqlRoundEvent.round_id, SqlRoundEvent.seq)
            ).scalars()
            for row in rows:
                out.setdefault(row.round_id, []).append(_flat(row))
        return out

    def recent(self, conversation_id: str, limit: int) -> list[str]:
        """The Conversation's *limit* Rounds with the newest last event, newest first."""
        last = func.max(SqlRoundEvent.at_ms)
        with self._session("round_events_recent") as session:
            return list(
                session.execute(
                    select(SqlRoundEvent.round_id)
                    .where(
                        SqlRoundEvent.workspace_id == current_workspace_id(),
                        SqlRoundEvent.conversation_id == conversation_id,
                    )
                    .group_by(SqlRoundEvent.round_id)
                    .order_by(last.desc())
                    .limit(limit)
                ).scalars()
            )

    def rounds_since(self, since_ms: int) -> list[tuple[str, str]]:
        """``(round id, conversation id)`` of each Round with an event since *since_ms* (epoch
        ms): the Rounds the :class:`~omnigent.superchat.rounds.record.Sweep` reads at start,
        settled or not, so one left unsettled across a restart is never forgotten."""
        with self._session("round_events_since") as session:
            rows = session.execute(
                select(SqlRoundEvent.round_id, SqlRoundEvent.conversation_id)
                .where(
                    SqlRoundEvent.workspace_id == current_workspace_id(),
                    SqlRoundEvent.at_ms >= since_ms,
                )
                .distinct()
            ).all()
        return [(round_id, conversation_id) for round_id, conversation_id in rows]

    def item_rounds(self, conversation_id: str, item_ids: Sequence[str]) -> dict[str, str]:
        """The Round each of *item_ids* opened or woke: ``{item id: round id}``.

        A person's message that opened a Round is that Round's id; a Helper's notice is the
        ``wake`` of the Round that Helper is in. Any other item is in neither.
        """
        if not item_ids:
            return {}
        wanted = set(item_ids)
        with self._session("round_events_items") as session:
            rows = session.execute(
                select(SqlRoundEvent.round_id, SqlRoundEvent.kind, SqlRoundEvent.body).where(
                    SqlRoundEvent.workspace_id == current_workspace_id(),
                    SqlRoundEvent.conversation_id == conversation_id,
                    or_(
                        and_(
                            SqlRoundEvent.kind == ROUND_OPENED,
                            SqlRoundEvent.round_id.in_(list(wanted)),
                        ),
                        SqlRoundEvent.kind == WAKE,
                    ),
                )
            ).all()
        found: dict[str, str] = {}
        for round_id, kind, body in rows:
            item_id = round_id if kind == ROUND_OPENED else json.loads(body).get("item_id")
            if item_id in wanted:
                found[item_id] = round_id
        return found

    def delete_conversation(self, conversation_id: str) -> None:
        """Drop every Round event of a deleted Conversation."""

        def write(session: Session) -> None:
            session.execute(
                delete(SqlRoundEvent).where(
                    SqlRoundEvent.workspace_id == current_workspace_id(),
                    SqlRoundEvent.conversation_id == conversation_id,
                )
            )

        run_write_transaction(self._session_immediate, "round_events_delete", write)
