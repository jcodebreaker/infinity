"""The rhythm's tables (ADR 0038): ``rhythm_tallies`` (owner x cell), ``rhythm_events`` and
``reach_rules`` (what the person said; Start over never touches it).

Only tallies and events are kept; no presence history. An event is kept once per message and
kind; adding one moves its message's cell by the change in the message's outcome
(:func:`omnigent.superchat.rhythm.score.outcome`), in the same transaction.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlReachRule,
    SqlRhythmEvent,
    SqlRhythmTally,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.superchat.rhythm.reach import Rule
from omnigent.superchat.rhythm.score import Tally, decayed, outcome

#: The tally row that only marks when the person last chose *Start over* (its ``updated_at``):
#: messages from before it are never scored again.
START_OVER_CELL = "start_over"
#: How far back the days-with-events count looks.
_DAYS_LOOKBACK_S = 90 * 24 * 3600


class SqlAlchemyRhythmStore:
    """SQLAlchemy-backed store for ``rhythm_tallies`` and ``rhythm_events``."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.rhythm_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.rhythm_store", immediate=True
        )

    def tallies(self, owner: str, *, now: int | None = None) -> dict[str, Tally]:
        """``cell -> Tally`` for the owner, decayed to *now*."""
        at = now if now is not None else now_epoch()
        with self._session("rhythm_tallies") as session:
            stmt = select(SqlRhythmTally).where(
                SqlRhythmTally.workspace_id == current_workspace_id(),
                SqlRhythmTally.owner == owner,
            )
            return {
                r.cell: Tally(
                    yes=decayed(r.yes, at - r.updated_at), no=decayed(r.no, at - r.updated_at)
                )
                for r in session.execute(stmt).scalars().all()
                if r.cell != START_OVER_CELL
            }

    def started_at(self, owner: str) -> int:
        """Epoch seconds the person last chose *Start over*, ``0`` when never."""
        with self._session("rhythm_started_at") as session:
            row = session.get(SqlRhythmTally, (current_workspace_id(), owner, START_OVER_CELL))
            return int(row.updated_at) if row is not None else 0

    def event_times(self, owner: str, *, now: int | None = None) -> list[int]:
        """Epoch seconds of the owner's behaviour events of the last 90 days (``sent`` is not
        behaviour), for the days-with-events count."""
        since = (now if now is not None else now_epoch()) - _DAYS_LOOKBACK_S
        with self._session("rhythm_event_times") as session:
            stmt = select(SqlRhythmEvent.at).where(
                SqlRhythmEvent.workspace_id == current_workspace_id(),
                SqlRhythmEvent.owner == owner,
                SqlRhythmEvent.at >= since,
                SqlRhythmEvent.event != "sent",
            )
            return [int(v) for v in session.execute(stmt).scalars().all()]

    def with_events(self, owner: str, item_ids: list[str]) -> set[str]:
        """Those of *item_ids* that have any event other than ``sent``."""
        if not item_ids:
            return set()
        with self._session("rhythm_with_events") as session:
            rows = session.execute(self._events(owner, item_ids)).scalars().all()
            return {str(r.item_id) for r in rows if r.event != "sent"}

    @staticmethod
    def _events(owner: str, item_ids: list[str]) -> Any:
        return select(SqlRhythmEvent).where(
            SqlRhythmEvent.workspace_id == current_workspace_id(),
            SqlRhythmEvent.owner == owner,
            SqlRhythmEvent.item_id.in_(item_ids),
        )

    def sent_topics(self, owner: str, item_ids: list[str]) -> dict[str, list[str]]:
        """``item_id -> topic keys`` its ``sent`` named, for those of *item_ids* that have one."""
        if not item_ids:
            return {}
        found: dict[str, list[str]] = {}
        with self._session("rhythm_sent_topics") as session:
            for row in session.execute(self._events(owner, item_ids)).scalars().all():
                if row.event != "sent" or not row.topic_keys:
                    continue
                try:
                    keys = json.loads(row.topic_keys)
                except ValueError:
                    continue
                if isinstance(keys, list):
                    found[str(row.item_id)] = [str(k) for k in keys if k]
        return found

    def add_event(
        self,
        owner: str,
        item_id: str,
        event: str,
        at: int,
        cell: str | None,
        *,
        topic_keys: list[str] | None = None,
    ) -> bool:
        """Keep *event* on the message (the first of each kind) and move its cell.

        The message's cell is the one its earlier events carry, else *cell*. *topic_keys* (on
        ``sent``) are every topic the message covers.

        :returns: ``False`` when the message already had this event.
        """

        def write(session: Session) -> bool:
            rows = session.execute(self._events(owner, [item_id])).scalars().all()
            have = {r.event for r in rows}
            if event in have:
                return False
            known = next((r.cell for r in rows if r.cell), None)
            target = known or cell
            session.add(
                SqlRhythmEvent(
                    owner=owner,
                    item_id=item_id,
                    event=event,
                    at=at,
                    cell=target,
                    topic_keys=json.dumps(sorted(set(topic_keys))) if topic_keys else None,
                )
            )
            before, after = outcome(have), outcome(have | {event})
            delta_yes, delta_no = after[0] - before[0], after[1] - before[1]
            if target and (delta_yes or delta_no):
                now = now_epoch()
                key = (current_workspace_id(), owner, target)
                tally = session.get(SqlRhythmTally, key)
                if tally is None:
                    tally = SqlRhythmTally(
                        owner=owner, cell=target, yes=0.0, no=0.0, updated_at=now
                    )
                    session.add(tally)
                age = now - tally.updated_at
                tally.yes = max(decayed(tally.yes, age) + delta_yes, 0.0)
                tally.no = max(decayed(tally.no, age) + delta_no, 0.0)
                tally.updated_at = now
            return True

        return run_write_transaction(self._session_immediate, "rhythm_add_event", write)

    def reset(self, owner: str) -> None:
        """*Start over*: forget everything learned about the owner, and remember when. The
        person's reach rules are what they said, not what was learned: they stay."""

        def write(session: Session) -> None:
            ws = current_workspace_id()
            session.execute(
                delete(SqlRhythmTally).where(
                    SqlRhythmTally.workspace_id == ws, SqlRhythmTally.owner == owner
                )
            )
            session.execute(
                delete(SqlRhythmEvent).where(
                    SqlRhythmEvent.workspace_id == ws, SqlRhythmEvent.owner == owner
                )
            )
            session.add(SqlRhythmTally(owner=owner, cell=START_OVER_CELL, updated_at=now_epoch()))

        run_write_transaction(self._session_immediate, "rhythm_reset", write)

    # ── reach rules ─────────────────────────────────────────────────────────

    def _owner_rules(self, owner: str) -> Any:
        return select(SqlReachRule).where(
            SqlReachRule.workspace_id == current_workspace_id(), SqlReachRule.owner == owner
        )

    def rules(self, owner: str, *, now: int | None = None) -> list[Rule]:
        """The owner's reach rules not yet ended at *now*, oldest first. Ended ones are deleted
        on the way (never read again)."""
        at = now if now is not None else now_epoch()
        with self._session("reach_rules") as session:
            rows = session.execute(self._owner_rules(owner)).scalars().all()
            found = [_rule(r) for r in rows]
        ended = [r.id for r in found if not r.active(at)]
        if ended:
            self.remove_rules(owner, ended)
        return sorted((r for r in found if r.active(at)), key=lambda r: r.created_at)

    def add_rule(self, owner: str, rule: Rule, *, replace: list[str] | None = None) -> None:
        """Keep *rule*, deleting the owner's rules *replace* in the same transaction."""

        def write(session: Session) -> None:
            if replace:
                session.execute(
                    delete(SqlReachRule).where(
                        SqlReachRule.workspace_id == current_workspace_id(),
                        SqlReachRule.owner == owner,
                        SqlReachRule.id.in_(replace),
                    )
                )
            session.add(
                SqlReachRule(
                    id=rule.id,
                    owner=owner,
                    kind=rule.kind,
                    params=json.dumps(dict(rule.params)),
                    said=rule.said,
                    created_at=rule.created_at,
                    expires_at=rule.expires_at,
                )
            )

        run_write_transaction(self._session_immediate, "reach_rules_add", write)

    def remove_rules(self, owner: str, ids: list[str]) -> int:
        """Delete the owner's rules *ids*; how many went."""
        if not ids:
            return 0

        def write(session: Session) -> int:
            result = session.execute(
                delete(SqlReachRule).where(
                    SqlReachRule.workspace_id == current_workspace_id(),
                    SqlReachRule.owner == owner,
                    SqlReachRule.id.in_(ids),
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

        return run_write_transaction(self._session_immediate, "reach_rules_remove", write)


def _rule(row: SqlReachRule) -> Rule:
    try:
        params = json.loads(row.params)
    except ValueError:
        params = {}
    return Rule(
        id=row.id,
        kind=row.kind,
        params=params if isinstance(params, dict) else {},
        said=row.said or "",
        created_at=int(row.created_at),
        expires_at=int(row.expires_at) if row.expires_at is not None else None,
    )
