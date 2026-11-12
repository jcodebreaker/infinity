"""Daily note store: one note per owner per local day (owner-scoped)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationItem,
    SqlConversationLabel,
    SqlDailyNote,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.daily_note import (
    SECTION_KEYS,
    DailyNote,
    apply_person_edit,
    empty_sections,
    merge_writer,
)

LOCAL_OWNER = "local"
# A pass holds the note's lease this long; a crashed pass is retried once it has expired.
PASS_LEASE_SECONDS = 1800
PASS_COLUMNS = ("finalized_at", "dreamed_at", "people_at")


def _lease_free(row: SqlDailyNote, now: int) -> bool:
    return row.claimed_at is None or row.claimed_at <= now - PASS_LEASE_SECONDS


def _to_entity(row: SqlDailyNote) -> DailyNote:
    try:
        raw = json.loads(row.sections)
    except ValueError:
        raw = {}
    sections = {**empty_sections(), **{k: str(v) for k, v in raw.items() if k in SECTION_KEYS}}
    try:
        edited = [k for k in json.loads(row.edited_sections) if k in SECTION_KEYS]
    except ValueError:
        edited = []
    return DailyNote(
        owner=row.owner,
        note_date=row.note_date,
        sections=sections,
        edited_sections=edited,
        edited_by_person=bool(row.edited_by_person),
        parent_session_id=row.parent_session_id,
        quiet_passes=row.quiet_passes,
        finalized_at=row.finalized_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        dreamed_at=row.dreamed_at,
        people_at=row.people_at,
        claimed_at=row.claimed_at,
    )


def _new_row(owner: str, note_date: str, now: int) -> SqlDailyNote:
    return SqlDailyNote(
        owner=owner,
        note_date=note_date,
        sections=json.dumps(empty_sections()),
        edited_sections="[]",
        edited_by_person=False,
        quiet_passes=0,
        created_at=now,
        updated_at=now,
    )


class SqlAlchemyDailyNoteStore:
    """SQLAlchemy-backed store for the ``daily_notes`` table."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.daily_note_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.daily_note_store", immediate=True
        )

    @staticmethod
    def owner_key(user_id: str | None) -> str:
        """The stored owner key for a user id (``None`` is single-user mode)."""
        return user_id or LOCAL_OWNER

    def get(self, owner: str, note_date: str) -> DailyNote | None:
        """Return the owner's note for a day, or ``None``."""
        with self._session("select_daily_note") as session:
            row = session.get(SqlDailyNote, (current_workspace_id(), owner, note_date))
            return _to_entity(row) if row else None

    def list_range(
        self, owner: str, *, date_from: str | None, date_to: str | None, limit: int = 60
    ) -> list[DailyNote]:
        """The owner's notes within an inclusive date range, newest first."""
        with self._session("list_daily_notes") as session:
            stmt = select(SqlDailyNote).where(
                SqlDailyNote.workspace_id == current_workspace_id(), SqlDailyNote.owner == owner
            )
            if date_from:
                stmt = stmt.where(SqlDailyNote.note_date >= date_from)
            if date_to:
                stmt = stmt.where(SqlDailyNote.note_date <= date_to)
            stmt = stmt.order_by(desc(SqlDailyNote.note_date)).limit(limit)
            return [_to_entity(r) for r in session.execute(stmt).scalars().all()]

    def _write(self, owner: str, note_date: str, op: str, mutate: Any) -> DailyNote:
        now = now_epoch()

        def write(session: Session) -> DailyNote:
            key = (current_workspace_id(), owner, note_date)
            row = session.get(SqlDailyNote, key)
            if row is None:
                row = _new_row(owner, note_date, now)
                session.add(row)
            mutate(row)
            row.updated_at = now
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, op, write)

    def write_sections(
        self,
        owner: str,
        note_date: str,
        sections: dict[str, object],
        *,
        parent_session_id: str | None = None,
    ) -> DailyNote:
        """A background writer's update: person-edited sections are appended to, never replaced."""

        def mutate(row: SqlDailyNote) -> None:
            current = _to_entity(row)
            merged = merge_writer(current.sections, current.edited_sections, sections)
            row.sections = json.dumps(merged)
            if parent_session_id:
                row.parent_session_id = parent_session_id

        return self._write(owner, note_date, "daily_note_writer", mutate)

    def person_edit(self, owner: str, note_date: str, sections: dict[str, object]) -> DailyNote:
        """The person's edit: the given sections win and are protected from the writer."""

        def mutate(row: SqlDailyNote) -> None:
            current = _to_entity(row)
            merged, edited = apply_person_edit(current.sections, current.edited_sections, sections)
            row.sections = json.dumps(merged)
            row.edited_sections = json.dumps(edited)
            row.edited_by_person = bool(edited)

        return self._write(owner, note_date, "daily_note_person_edit", mutate)

    def claim_quiet_pass(
        self, owner: str, note_date: str, *, max_per_day: int, parent_session_id: str
    ) -> bool:
        """Count one quiet-moment pass for the day; ``False`` (and no change) at the cap."""
        claimed = False

        def mutate(row: SqlDailyNote) -> None:
            nonlocal claimed
            if row.quiet_passes >= max_per_day:
                return
            row.quiet_passes += 1
            row.parent_session_id = parent_session_id
            claimed = True

        self._write(owner, note_date, "daily_note_claim_pass", mutate)
        return claimed

    def claim_finalize(self, owner: str, note_date: str) -> bool:
        """Take the lease for the finalizing pass; ``False`` when done or another pass holds it."""
        return self._claim(owner, note_date, "finalized_at")

    def claim_pass(self, owner: str, note_date: str, column: str) -> bool:
        """Take the lease for a nightly pass (``dreamed_at`` or ``people_at``) of the day."""
        if column not in ("dreamed_at", "people_at"):
            raise ValueError(column)
        return self._claim(owner, note_date, column)

    def _claim(self, owner: str, note_date: str, column: str) -> bool:
        claimed = False

        def mutate(row: SqlDailyNote) -> None:
            nonlocal claimed
            now = now_epoch()
            if getattr(row, column) is None and _lease_free(row, now):
                row.claimed_at = now
                claimed = True

        self._write(owner, note_date, f"daily_note_claim_{column}", mutate)
        return claimed

    def complete_pass(self, owner: str, note_date: str, column: str) -> None:
        """Mark a pass done (one of :data:`PASS_COLUMNS`) and free the lease."""
        if column not in PASS_COLUMNS:
            raise ValueError(column)

        def mutate(row: SqlDailyNote) -> None:
            setattr(row, column, now_epoch())
            row.claimed_at = None

        self._write(owner, note_date, f"daily_note_complete_{column}", mutate)

    def pending_nightly(self, *, finalized_after: int, finalized_before: int) -> list[DailyNote]:
        """Finalized notes (in the window) with content still missing a Dreaming or People pass."""
        with self._session("list_daily_notes_nightly") as session:
            stmt = (
                select(SqlDailyNote)
                .where(
                    SqlDailyNote.workspace_id == current_workspace_id(),
                    SqlDailyNote.finalized_at.is_not(None),
                    SqlDailyNote.finalized_at >= finalized_after,
                    SqlDailyNote.finalized_at <= finalized_before,
                    SqlDailyNote.parent_session_id.is_not(None),
                    (SqlDailyNote.dreamed_at.is_(None)) | (SqlDailyNote.people_at.is_(None)),
                )
                .order_by(SqlDailyNote.note_date)
                .limit(200)
            )
            notes = [_to_entity(r) for r in session.execute(stmt).scalars().all()]
            return [n for n in notes if any(text.strip() for text in n.sections.values())]

    def pending_finalize(self, *, limit: int = 200) -> list[DailyNote]:
        """Unfinalized notes that had at least one pass and a chat to run in, oldest first."""
        with self._session("list_daily_notes_pending") as session:
            stmt = (
                select(SqlDailyNote)
                .where(
                    SqlDailyNote.workspace_id == current_workspace_id(),
                    SqlDailyNote.finalized_at.is_(None),
                    SqlDailyNote.quiet_passes > 0,
                    SqlDailyNote.parent_session_id.is_not(None),
                )
                .order_by(SqlDailyNote.note_date)
                .limit(limit)
            )
            return [_to_entity(r) for r in session.execute(stmt).scalars().all()]

    def recent_user_messages(self, *, since: int) -> dict[str, list[int]]:
        """Person messages (epoch seconds, oldest first) per live Super Chat since ``since``.

        Derived from the persisted conversation items so idleness survives a restart.
        Sub-agents and archived chats are left out.
        """
        from omnigent.context.labels import (
            CONTEXT_MODE_LABEL,
            SUBAGENT_LABEL_KEY,
            SUPERSIDE_CHAT_MODE_VALUE,
        )

        workspace = current_workspace_id()
        with self._session("select_recent_user_messages") as session:
            ids = (
                select(SqlConversationLabel.conversation_id)
                .where(
                    SqlConversationLabel.workspace_id == workspace,
                    SqlConversationLabel.key == CONTEXT_MODE_LABEL,
                    SqlConversationLabel.value == SUPERSIDE_CHAT_MODE_VALUE,
                )
                .scalar_subquery()
            )
            excluded = (
                select(SqlConversationLabel.conversation_id)
                .where(
                    SqlConversationLabel.workspace_id == workspace,
                    SqlConversationLabel.key == SUBAGENT_LABEL_KEY,
                )
                .scalar_subquery()
            )
            stmt = (
                select(
                    SqlConversationItem.conversation_id,
                    SqlConversationItem.created_at,
                    SqlConversationItem.data,
                )
                .join(
                    SqlConversation,
                    (SqlConversation.workspace_id == SqlConversationItem.workspace_id)
                    & (SqlConversation.id == SqlConversationItem.conversation_id),
                )
                .where(
                    SqlConversationItem.workspace_id == workspace,
                    SqlConversationItem.type == 1,
                    SqlConversationItem.created_at >= since,
                    SqlConversation.parent_conversation_id.is_(None),
                    SqlConversation.archived.is_(False),
                    SqlConversationItem.conversation_id.in_(ids),
                    SqlConversationItem.conversation_id.not_in(excluded),
                )
                .order_by(SqlConversationItem.created_at)
            )
            found: dict[str, list[int]] = {}
            for chat_id, created_at, data in session.execute(stmt).all():
                try:
                    role = json.loads(data).get("role")
                except (ValueError, AttributeError):
                    continue
                if role == "user":
                    found.setdefault(str(chat_id), []).append(created_at)
            return found
