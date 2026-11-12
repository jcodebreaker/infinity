"""The Lookout's tables (ADR 0038): signals, each person's next wake, the wake ledger, terms.

Owner-scoped and workspace-scoped like every feature store, except the two sweeps the ticker
makes across workspaces (:meth:`due_owners`, :meth:`stuck_wakes`), which return the workspace
with each row so the caller works inside its scope.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationLabel,
    SqlLookoutOwner,
    SqlLookoutSignal,
    SqlLookoutTerm,
    SqlLookoutWake,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.superchat.lookout.rules import COMMON_WINDOW_DAYS
from omnigent.superchat.lookout.signals import FORGET_ON_SETTLE, NOW, SETTLE_SECONDS, Signal

#: Wake statuses while a run holds its signals.
IN_FLIGHT = ("claimed", "running")
#: How long settled signals and finished wakes are kept.
KEEP_SECONDS = 60 * 24 * 3600
#: The longest term the counts keep (the column's width).
TERM_CHARS = 128


def day_of(epoch: int) -> int:
    """The UTC day number the term counts are kept by."""
    return epoch // 86400


@dataclass(frozen=True)
class StoredSignal:
    """A signal as stored, with its id and claim."""

    id: str
    owner: str
    kind: str
    topic_key: str | None
    subject: str
    detail: dict[str, Any]
    conversation_id: str | None
    due_at: int
    stale_after: int
    urgency: str
    dedupe_key: str
    claimed_run_id: str | None
    created_at: int


@dataclass(frozen=True)
class Wake:
    """One wake that reached a run (``kind`` ``wake`` or ``brief``)."""

    id: str
    owner: str
    kind: str
    scheduled_task_id: str | None
    conversation_id: str | None
    run_conversation_id: str | None
    status: str
    urgency: str
    topic_keys: list[str]
    item_id: str | None
    started_at: int
    state: str | None = None


def _signal(row: SqlLookoutSignal) -> StoredSignal:
    try:
        detail = json.loads(row.detail)
    except ValueError:
        detail = {}
    return StoredSignal(
        id=row.id,
        owner=row.owner,
        kind=row.kind,
        topic_key=row.topic_key,
        subject=row.subject,
        detail=detail if isinstance(detail, dict) else {},
        conversation_id=row.conversation_id,
        due_at=row.due_at,
        stale_after=row.stale_after,
        urgency=row.urgency,
        dedupe_key=row.dedupe_key,
        claimed_run_id=row.claimed_run_id,
        created_at=row.created_at,
    )


def _wake(row: SqlLookoutWake) -> Wake:
    try:
        keys = json.loads(row.topic_keys)
    except ValueError:
        keys = []
    return Wake(
        id=row.id,
        owner=row.owner,
        kind=row.kind,
        scheduled_task_id=row.scheduled_task_id,
        conversation_id=row.conversation_id,
        run_conversation_id=row.run_conversation_id,
        status=row.status,
        urgency=row.urgency,
        topic_keys=[str(k) for k in keys] if isinstance(keys, list) else [],
        item_id=row.item_id,
        started_at=row.started_at,
        state=row.state,
    )


def wake_time(signal: Signal, now: int) -> int:
    """When a new *signal* should wake its Lookout: at once for ``now``, else at its due time
    but never before the settle delay (a burst is one wake)."""
    if signal.urgency == NOW:
        return max(signal.due_at, now)
    return max(signal.due_at, now + SETTLE_SECONDS)


class SqlAlchemyLookoutStore:
    """SQLAlchemy-backed store for the ``lookout_*`` tables."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.lookout_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.lookout_store", immediate=True
        )

    def _write(self, name: str, fn: Any) -> Any:
        return run_write_transaction(self._session_immediate, name, fn)

    # ------------------------------------------------------------------ signals

    def add(self, signal: Signal, *, now: int | None = None) -> str:
        """Store *signal*, folding it into the owner's waiting one with the same key, and move
        the owner's next wake earlier when it is due sooner.

        A repeat replaces the waiting signal's text, times and topic (the newest word on one
        thing); a signal a run already claimed is updated the same way and stays claimed.

        :returns: The signal's id.
        """
        instant = now if now is not None else now_epoch()
        detail = json.dumps(dict(signal.detail), ensure_ascii=False, default=str)

        def write(session: Session) -> str:
            workspace = current_workspace_id()
            row = (
                session.execute(
                    select(SqlLookoutSignal)
                    .where(
                        SqlLookoutSignal.workspace_id == workspace,
                        SqlLookoutSignal.owner == signal.owner,
                        SqlLookoutSignal.dedupe_key == signal.dedupe_key,
                        SqlLookoutSignal.done_at.is_(None),
                    )
                    .order_by(desc(SqlLookoutSignal.created_at))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if row is None:
                row = SqlLookoutSignal(
                    workspace_id=workspace,
                    id=uuid.uuid4().hex,
                    owner=signal.owner,
                    dedupe_key=signal.dedupe_key,
                    created_at=instant,
                )
                session.add(row)
            row.kind = signal.kind
            row.topic_key = signal.topic_key
            row.subject = signal.subject
            row.detail = detail
            row.conversation_id = signal.conversation_id or row.conversation_id
            row.due_at = signal.due_at
            row.stale_after = signal.stale_after
            row.urgency = signal.urgency
            row.updated_at = instant
            self._wake_at(
                session,
                signal.owner,
                wake_time(signal, instant),
                instant,
                conversation_id=signal.conversation_id,
            )
            session.flush()
            return row.id

        return self._write("lookout_add_signal", write)

    def remove(self, owner: str, dedupe_key: str, *, now: int | None = None) -> int:
        """Settle the owner's waiting signals with *dedupe_key* as ``removed`` (a claimed one
        too: it is then never marked sent).

        :returns: How many were removed.
        """
        instant = now if now is not None else now_epoch()

        def write(session: Session) -> int:
            result = session.execute(
                update(SqlLookoutSignal)
                .where(
                    SqlLookoutSignal.workspace_id == current_workspace_id(),
                    SqlLookoutSignal.owner == owner,
                    SqlLookoutSignal.dedupe_key == dedupe_key,
                    SqlLookoutSignal.done_at.is_(None),
                )
                .values(done_at=instant, outcome="removed", updated_at=instant)
            )
            self._forget(session)
            return int(result.rowcount or 0)

        return self._write("lookout_remove_signal", write)

    def waiting(self, owner: str) -> list[StoredSignal]:
        """The owner's unsettled, unclaimed signals, earliest due first (future ones too)."""
        with self._session("lookout_waiting") as session:
            rows = session.execute(
                select(SqlLookoutSignal)
                .where(
                    SqlLookoutSignal.workspace_id == current_workspace_id(),
                    SqlLookoutSignal.owner == owner,
                    SqlLookoutSignal.done_at.is_(None),
                    SqlLookoutSignal.claimed_run_id.is_(None),
                )
                .order_by(SqlLookoutSignal.due_at)
                .limit(500)
            ).scalars()
            return [_signal(r) for r in rows]

    def claimed(self, wake_id: str) -> list[StoredSignal]:
        """The unsettled signals *wake_id* holds, earliest due first."""
        with self._session("lookout_claimed") as session:
            rows = session.execute(
                select(SqlLookoutSignal)
                .where(
                    SqlLookoutSignal.workspace_id == current_workspace_id(),
                    SqlLookoutSignal.claimed_run_id == wake_id,
                    SqlLookoutSignal.done_at.is_(None),
                )
                .order_by(SqlLookoutSignal.due_at)
            ).scalars()
            return [_signal(r) for r in rows]

    def settle(self, ids: Sequence[str], outcome: str, *, now: int | None = None) -> None:
        """Settle signals *ids* with *outcome* (``stale``, ``unmatched``, ``muted`` ...)."""
        if not ids:
            return
        instant = now if now is not None else now_epoch()

        def write(session: Session) -> None:
            session.execute(
                update(SqlLookoutSignal)
                .where(
                    SqlLookoutSignal.workspace_id == current_workspace_id(),
                    SqlLookoutSignal.id.in_(list(ids)),
                    SqlLookoutSignal.done_at.is_(None),
                )
                .values(done_at=instant, outcome=outcome[:16], updated_at=instant)
            )
            self._forget(session)

        self._write("lookout_settle", write)

    def set_topic(self, signal_id: str, topic_key: str) -> None:
        """Record the topic the code check matched a signal to."""

        def write(session: Session) -> None:
            row = session.get(SqlLookoutSignal, (current_workspace_id(), signal_id))
            if row is not None:
                row.topic_key = topic_key[:128]

        self._write("lookout_set_topic", write)

    def annotate(
        self, signal_id: str, fields: dict[str, Any], *, topic_key: str | None = None
    ) -> None:
        """Merge *fields* into a signal's detail (the code's match hint, the Judge's verdict),
        read and written in one transaction so a newer repeat's text is never overwritten; with
        *topic_key*, also give it that topic when it has none."""

        def write(session: Session) -> None:
            row = session.get(SqlLookoutSignal, (current_workspace_id(), signal_id))
            if row is None:
                return
            try:
                detail = json.loads(row.detail)
            except ValueError:
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            detail.update(fields)
            row.detail = json.dumps(detail, ensure_ascii=False, default=str)
            if topic_key and not row.topic_key:
                row.topic_key = topic_key[:128]

        self._write("lookout_annotate", write)

    # ------------------------------------------------------------------ the next wake

    @staticmethod
    def _wake_at(
        session: Session, owner: str, at: int, now: int, *, conversation_id: str | None = None
    ) -> None:
        """Move the owner's next wake to *at* when that is earlier (made when missing), and
        keep *conversation_id* as their Conversation when given."""
        row = session.get(SqlLookoutOwner, (current_workspace_id(), owner))
        if row is None:
            session.add(
                SqlLookoutOwner(
                    owner=owner, conversation_id=conversation_id, next_wake_at=at, updated_at=now
                )
            )
            return
        if row.next_wake_at is None or at < row.next_wake_at:
            row.next_wake_at = at
            row.updated_at = now
        if conversation_id:
            row.conversation_id = conversation_id

    def wake_by(self, owner: str, at: int) -> None:
        """Move the owner's next wake to *at* when that is earlier (their lease untouched)."""
        instant = now_epoch()
        self._write("lookout_wake_by", lambda session: self._wake_at(session, owner, at, instant))

    def set_next_wake(self, owner: str, at: int | None) -> None:
        """Set the owner's next wake (``None``: an empty tray) and drop the lease."""
        instant = now_epoch()

        def write(session: Session) -> None:
            row = session.get(SqlLookoutOwner, (current_workspace_id(), owner))
            if row is None:
                session.add(SqlLookoutOwner(owner=owner, next_wake_at=at, updated_at=instant))
                return
            row.next_wake_at, row.lease_until, row.updated_at = at, None, instant

        self._write("lookout_set_next_wake", write)

    def due_owners(self, now: int, *, limit: int = 50) -> list[tuple[int, str]]:
        """``(workspace, owner)`` whose next wake is due and who no replica holds, every
        workspace, earliest first."""
        with self._session("lookout_due_owners") as session:
            rows = session.execute(
                select(SqlLookoutOwner.workspace_id, SqlLookoutOwner.owner)
                .where(
                    SqlLookoutOwner.next_wake_at.is_not(None),
                    SqlLookoutOwner.next_wake_at <= now,
                    or_(SqlLookoutOwner.lease_until.is_(None), SqlLookoutOwner.lease_until < now),
                )
                .order_by(SqlLookoutOwner.next_wake_at)
                .limit(limit)
            ).all()
            return [(int(w), str(o)) for w, o in rows]

    def lease(self, owner: str, now: int, seconds: int) -> bool:
        """Hold the owner's wake for *seconds* when it is due and free; ``False`` when another
        replica holds it or it is not due any more."""

        def write(session: Session) -> bool:
            result = session.execute(
                update(SqlLookoutOwner)
                .where(
                    SqlLookoutOwner.workspace_id == current_workspace_id(),
                    SqlLookoutOwner.owner == owner,
                    SqlLookoutOwner.next_wake_at.is_not(None),
                    SqlLookoutOwner.next_wake_at <= now,
                    or_(SqlLookoutOwner.lease_until.is_(None), SqlLookoutOwner.lease_until < now),
                )
                .values(lease_until=now + seconds, updated_at=now)
            )
            return bool(result.rowcount)

        return self._write("lookout_lease", write)

    def conversation_of(self, owner: str) -> str | None:
        """The owner's Conversation as last seen from a source, if any."""
        with self._session("lookout_owner") as session:
            row = session.get(SqlLookoutOwner, (current_workspace_id(), owner))
            return row.conversation_id if row is not None else None

    def remember_conversation(self, owner: str, conversation_id: str) -> None:
        """Keep *conversation_id* as the owner's Conversation."""
        instant = now_epoch()

        def write(session: Session) -> None:
            row = session.get(SqlLookoutOwner, (current_workspace_id(), owner))
            if row is None:
                session.add(
                    SqlLookoutOwner(
                        owner=owner, conversation_id=conversation_id, updated_at=instant
                    )
                )
            else:
                row.conversation_id = conversation_id

        self._write("lookout_remember_conversation", write)

    def conversations(self, label_key: str) -> list[str]:
        """Ids of the unarchived sessions carrying *label_key* = ``true``, newest first (the
        fallback when no source named the person's Conversation)."""
        workspace = current_workspace_id()
        with self._session("lookout_conversations") as session:
            rows = session.execute(
                select(SqlConversation.id)
                .join(
                    SqlConversationLabel,
                    (SqlConversationLabel.workspace_id == SqlConversation.workspace_id)
                    & (SqlConversationLabel.conversation_id == SqlConversation.id),
                )
                .where(
                    SqlConversation.workspace_id == workspace,
                    SqlConversationLabel.key == label_key,
                    SqlConversationLabel.value == "true",
                    SqlConversation.archived.is_(False),
                )
                .order_by(desc(SqlConversation.updated_at))
                .limit(200)
            ).scalars()
            return list(rows)

    # ------------------------------------------------------------------ wakes

    def claim(
        self,
        *,
        owner: str,
        kind: str,
        signal_ids: Sequence[str],
        urgency: str,
        topic_keys: Sequence[str],
        conversation_id: str | None,
        scheduled_task_id: str | None,
        now: int | None = None,
        state: str | None = None,
    ) -> tuple[str, int]:
        """Open a wake and bind the still-waiting signals of *signal_ids* to it (*state*: what
        the person was doing when the rhythm was checked, kept for the message's ``sent``).

        :returns: ``(wake id, signals claimed)``.
        """
        instant = now if now is not None else now_epoch()
        wake_id = uuid.uuid4().hex

        def write(session: Session) -> int:
            workspace = current_workspace_id()
            session.add(
                SqlLookoutWake(
                    workspace_id=workspace,
                    id=wake_id,
                    owner=owner,
                    kind=kind,
                    scheduled_task_id=scheduled_task_id,
                    conversation_id=conversation_id,
                    status="claimed",
                    urgency=urgency,
                    state=state,
                    topic_keys=json.dumps(sorted(set(topic_keys))),
                    started_at=instant,
                )
            )
            if not signal_ids:
                return 0
            result = session.execute(
                update(SqlLookoutSignal)
                .where(
                    SqlLookoutSignal.workspace_id == workspace,
                    SqlLookoutSignal.owner == owner,
                    SqlLookoutSignal.id.in_(list(signal_ids)),
                    SqlLookoutSignal.done_at.is_(None),
                    SqlLookoutSignal.claimed_run_id.is_(None),
                )
                .values(claimed_run_id=wake_id, updated_at=instant)
            )
            return int(result.rowcount or 0)

        claimed = self._write("lookout_claim", write)
        return wake_id, claimed

    def get_wake(self, wake_id: str) -> Wake | None:
        """One wake, or ``None``."""
        with self._session("lookout_wake") as session:
            row = session.get(SqlLookoutWake, (current_workspace_id(), wake_id))
            return _wake(row) if row is not None else None

    def wake_for_task(
        self, scheduled_task_id: str, *, run_conversation_id: str | None
    ) -> Wake | None:
        """The in-flight wake of a task's run: the one bound to *run_conversation_id*, else the
        newest one not bound to any run yet."""
        with self._session("lookout_wake_for_task") as session:
            base = select(SqlLookoutWake).where(
                SqlLookoutWake.workspace_id == current_workspace_id(),
                SqlLookoutWake.scheduled_task_id == scheduled_task_id,
                SqlLookoutWake.status.in_(IN_FLIGHT),
            )
            if run_conversation_id:
                bound = (
                    session.execute(
                        base.where(SqlLookoutWake.run_conversation_id == run_conversation_id)
                    )
                    .scalars()
                    .first()
                )
                if bound is not None:
                    return _wake(bound)
            row = (
                session.execute(
                    base.where(SqlLookoutWake.run_conversation_id.is_(None))
                    .order_by(desc(SqlLookoutWake.started_at))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            return _wake(row) if row is not None else None

    def in_flight(self, owner: str) -> list[Wake]:
        """The owner's wakes whose run still holds signals."""
        with self._session("lookout_in_flight") as session:
            rows = session.execute(
                select(SqlLookoutWake).where(
                    SqlLookoutWake.workspace_id == current_workspace_id(),
                    SqlLookoutWake.owner == owner,
                    SqlLookoutWake.status.in_(IN_FLIGHT),
                )
            ).scalars()
            return [_wake(r) for r in rows]

    def bind_run(self, wake_id: str, run_conversation_id: str) -> None:
        """The wake's run started in session *run_conversation_id*."""

        def write(session: Session) -> None:
            row = session.get(SqlLookoutWake, (current_workspace_id(), wake_id))
            if row is not None and row.status in IN_FLIGHT:
                row.status = "running"
                row.run_conversation_id = run_conversation_id

        self._write("lookout_bind_run", write)

    def release(self, wake_id: str, *, now: int | None = None) -> bool:
        """The wake's run failed or was reaped: its signals wait again, never lost.

        :returns: ``False`` when the wake had already ended.
        """
        instant = now if now is not None else now_epoch()

        def write(session: Session) -> bool:
            workspace = current_workspace_id()
            row = session.get(SqlLookoutWake, (workspace, wake_id))
            if row is None or row.status not in IN_FLIGHT:
                return False
            row.status, row.finished_at = "released", instant
            session.execute(
                update(SqlLookoutSignal)
                .where(
                    SqlLookoutSignal.workspace_id == workspace,
                    SqlLookoutSignal.claimed_run_id == wake_id,
                    SqlLookoutSignal.done_at.is_(None),
                )
                .values(claimed_run_id=None, updated_at=instant)
            )
            return True

        return self._write("lookout_release", write)

    def fail(
        self, wake_id: str, *, max_attempts: int, now: int | None = None
    ) -> list[StoredSignal] | None:
        """The wake's run failed (an error, a policy block, a budget): release its signals for
        another try, counting the attempt on each (``detail["_attempts"]``); a signal that
        reached *max_attempts* is settled ``failed`` instead.

        :returns: The signals settled ``failed`` (their topics get "New"), or ``None`` when the
            wake had already ended.
        """
        instant = now if now is not None else now_epoch()

        def write(session: Session) -> list[StoredSignal] | None:
            workspace = current_workspace_id()
            row = session.get(SqlLookoutWake, (workspace, wake_id))
            if row is None or row.status not in IN_FLIGHT:
                return None
            row.status, row.finished_at = "released", instant
            given_up: list[StoredSignal] = []
            held = session.execute(
                select(SqlLookoutSignal).where(
                    SqlLookoutSignal.workspace_id == workspace,
                    SqlLookoutSignal.claimed_run_id == wake_id,
                    SqlLookoutSignal.done_at.is_(None),
                )
            ).scalars()
            for signal in held:
                try:
                    detail = json.loads(signal.detail)
                except ValueError:
                    detail = {}
                if not isinstance(detail, dict):
                    detail = {}
                attempts = int(detail.get("_attempts") or 0) + 1
                detail["_attempts"] = attempts
                signal.detail = json.dumps(detail)
                signal.claimed_run_id = None
                signal.updated_at = instant
                if attempts >= max_attempts:
                    signal.done_at, signal.outcome = instant, "failed"
                    given_up.append(_signal(signal))
            session.flush()
            self._forget(session)
            return given_up

        return self._write("lookout_fail", write)

    def finish(self, wake_id: str, *, item_id: str | None, now: int | None = None) -> bool:
        """The wake's run ended: ``sent`` with the stored message *item_id*, or ``nothing``
        (``item_id`` ``None``: nothing worth saying). Its signals are settled either way.

        :returns: ``False`` when the wake had already ended (a repeat is a no-op).
        """
        instant = now if now is not None else now_epoch()
        outcome = "sent" if item_id else "nothing"

        def write(session: Session) -> bool:
            workspace = current_workspace_id()
            row = session.get(SqlLookoutWake, (workspace, wake_id))
            if row is None or row.status not in IN_FLIGHT:
                return False
            row.status, row.finished_at, row.item_id = outcome, instant, item_id
            session.execute(
                update(SqlLookoutSignal)
                .where(
                    SqlLookoutSignal.workspace_id == workspace,
                    SqlLookoutSignal.claimed_run_id == wake_id,
                    SqlLookoutSignal.done_at.is_(None),
                )
                .values(
                    done_at=instant,
                    sent_at=instant if item_id else None,
                    outcome=outcome,
                    updated_at=instant,
                )
            )
            self._forget(session)
            self._prune(session, row.owner, instant)
            return True

        return self._write("lookout_finish", write)

    def stuck_wakes(self, before: int) -> list[tuple[int, str]]:
        """``(workspace, wake id)`` of runs claimed before *before* that never ended, every
        workspace: the backstop for a run nobody reported."""
        with self._session("lookout_stuck") as session:
            rows = session.execute(
                select(SqlLookoutWake.workspace_id, SqlLookoutWake.id).where(
                    SqlLookoutWake.status.in_(IN_FLIGHT), SqlLookoutWake.started_at < before
                )
            ).all()
            return [(int(w), str(i)) for w, i in rows]

    @staticmethod
    def _forget(session: Session) -> None:
        """Delete the settled signals of kinds kept only while they wait (mail, ADR 0039)."""
        session.execute(
            delete(SqlLookoutSignal).where(
                SqlLookoutSignal.workspace_id == current_workspace_id(),
                SqlLookoutSignal.kind.in_(sorted(FORGET_ON_SETTLE)),
                SqlLookoutSignal.done_at.is_not(None),
            )
        )

    @staticmethod
    def _prune(session: Session, owner: str, now: int) -> None:
        workspace = current_workspace_id()
        session.execute(
            delete(SqlLookoutSignal).where(
                SqlLookoutSignal.workspace_id == workspace,
                SqlLookoutSignal.owner == owner,
                SqlLookoutSignal.done_at < now - KEEP_SECONDS,
            )
        )
        session.execute(
            delete(SqlLookoutWake).where(
                SqlLookoutWake.workspace_id == workspace,
                SqlLookoutWake.owner == owner,
                SqlLookoutWake.finished_at < now - KEEP_SECONDS,
            )
        )

    # ------------------------------------------------------------------ term counts

    def term_counts(self, owner: str, terms: Sequence[str]) -> dict[str, tuple[int, int]]:
        """``term -> (signals containing it, signals checked while watched)`` over the last
        :data:`COMMON_WINDOW_DAYS` UTC days. Terms are normalized; one with no rows is left out."""
        keys = list({t[:TERM_CHARS] for t in terms if t})
        if not keys:
            return {}
        since = day_of(now_epoch()) - COMMON_WINDOW_DAYS + 1
        row = SqlLookoutTerm
        with self._session("lookout_terms") as session:
            rows = session.execute(
                select(row.term, func.sum(row.hits), func.sum(row.items))
                .where(
                    row.workspace_id == current_workspace_id(),
                    row.owner == owner,
                    row.day >= since,
                    row.term.in_(keys),
                )
                .group_by(row.term)
            ).all()
            return {term: (int(hits or 0), int(items or 0)) for term, hits, items in rows}

    def add_terms(self, owner: str, terms: dict[str, tuple[int, int]]) -> None:
        """Add today's ``term -> (hits, items)`` to the counts; drop days past the window."""
        today = day_of(now_epoch())

        def write(session: Session) -> None:
            workspace = current_workspace_id()
            for term, (hits, items) in terms.items():
                key = term[:TERM_CHARS]
                if not key or items <= 0:
                    continue
                counted = session.get(SqlLookoutTerm, (workspace, owner, today, key))
                if counted is None:
                    session.add(
                        SqlLookoutTerm(
                            workspace_id=workspace,
                            owner=owner,
                            day=today,
                            term=key,
                            items=items,
                            hits=hits,
                        )
                    )
                else:
                    counted.items += items
                    counted.hits += hits
            session.execute(
                delete(SqlLookoutTerm).where(
                    SqlLookoutTerm.workspace_id == workspace,
                    SqlLookoutTerm.owner == owner,
                    SqlLookoutTerm.day <= today - COMMON_WINDOW_DAYS,
                )
            )

        self._write("lookout_add_terms", write)
