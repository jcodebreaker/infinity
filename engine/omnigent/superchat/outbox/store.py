"""The outbox tables (ADR 0036): proactive messages, channel cursors and *Not useful*."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    InvalidUuidError,
    SqlOutboxCursor,
    SqlOutboxFeedback,
    SqlOutboxItem,
    current_workspace_id,
    uuid_to_bytes,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.server.auth import RESERVED_USER_LOCAL

#: The channel this wave builds; its cursor is the person's last seen proactive message.
WEB_CHANNEL = "web"
_REASON_MAX = 1000


def _is_item_id(value: str) -> bool:
    try:
        uuid_to_bytes(value)
    except InvalidUuidError:
        return False
    return True


def owner_key(user_id: str | None) -> str:
    """The stored owner for a user id (``local`` when auth is off)."""
    return RESERVED_USER_LOCAL if user_id in (None, RESERVED_USER_LOCAL) else str(user_id)


@dataclass(frozen=True)
class OutboxRow:
    """One proactive message (or, while ``item_id`` is ``None``, the turn that will write it)."""

    id: str
    owner: str
    conversation_id: str
    kind: str
    urgency: str
    created_at_ms: int
    item_id: str | None = None
    response_id: str | None = None
    topic_key: str | None = None
    source: dict[str, str] = field(default_factory=dict)
    title: str = ""
    line: str = ""
    open: bool = True

    def label(self) -> dict[str, Any]:
        """The message's ``proactive`` label: ``{kind, topic_key?, urgency}``."""
        return {
            "kind": self.kind,
            **({"topic_key": self.topic_key} if self.topic_key else {}),
            "urgency": self.urgency,
        }

    def to_wire(self) -> dict[str, Any]:
        """The row as the outbox routes serve it (``created_at`` in epoch seconds)."""
        return {
            "item_id": self.item_id,
            "conversation_id": self.conversation_id,
            "kind": self.kind,
            "topic_key": self.topic_key,
            "urgency": self.urgency,
            "title": self.title,
            "line": self.line,
            "created_at": self.created_at_ms // 1000,
        }


def _row(row: SqlOutboxItem) -> OutboxRow:
    try:
        source = json.loads(row.source)
    except ValueError:
        source = {}
    return OutboxRow(
        id=row.id,
        owner=row.owner,
        conversation_id=row.conversation_id,
        kind=row.kind,
        urgency=row.urgency,
        created_at_ms=row.created_at_ms,
        item_id=row.item_id,
        response_id=row.response_id,
        topic_key=row.topic_key,
        source=source if isinstance(source, dict) else {},
        title=row.title,
        line=row.line,
        open=bool(row.open),
    )


def _brief(row: OutboxRow) -> dict[str, Any]:
    """What another capability reads of a proactive message (through core)."""
    return {
        "item_id": str(row.item_id),
        "kind": row.kind,
        "topic_key": row.topic_key,
        "created_at_ms": row.created_at_ms,
    }


class SqlAlchemyOutboxStore:
    """SQLAlchemy-backed store for ``outbox_items``, ``outbox_cursors`` and ``outbox_feedback``."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.outbox_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.outbox_store", immediate=True
        )

    def _items(self) -> Any:
        return select(SqlOutboxItem).where(SqlOutboxItem.workspace_id == current_workspace_id())

    # ── rows ────────────────────────────────────────────────────────────────

    def open_row(self, row: OutboxRow) -> OutboxRow:
        """Store *row* as its Conversation's one open row (any other open one is closed)."""

        def write(session: Session) -> OutboxRow:
            session.execute(
                update(SqlOutboxItem)
                .where(
                    SqlOutboxItem.workspace_id == current_workspace_id(),
                    SqlOutboxItem.conversation_id == row.conversation_id,
                    SqlOutboxItem.open.is_(True),
                )
                .values(open=False)
            )
            session.add(
                SqlOutboxItem(
                    id=row.id,
                    owner=row.owner,
                    conversation_id=row.conversation_id,
                    kind=row.kind,
                    topic_key=row.topic_key,
                    urgency=row.urgency,
                    source=json.dumps(row.source),
                    created_at_ms=row.created_at_ms,
                    open=True,
                )
            )
            return row

        return run_write_transaction(self._session_immediate, "outbox_open", write)

    def record(self, row: OutboxRow) -> OutboxRow:
        """Store *row*, already bound to its message and closed: a proactive message engine
        code posted itself (ADR 0038). A repeat for the same message is a no-op."""

        def write(session: Session) -> OutboxRow:
            found = (
                session.execute(self._items().where(SqlOutboxItem.item_id == row.item_id).limit(1))
                .scalars()
                .first()
            )
            if found is not None:
                return _row(found)
            session.add(
                SqlOutboxItem(
                    id=row.id,
                    owner=row.owner,
                    conversation_id=row.conversation_id,
                    item_id=row.item_id,
                    response_id=row.response_id,
                    kind=row.kind,
                    topic_key=row.topic_key,
                    urgency=row.urgency,
                    source=json.dumps(row.source),
                    title=row.title,
                    line=row.line,
                    created_at_ms=row.created_at_ms,
                    open=False,
                )
            )
            return row

        return run_write_transaction(self._session_immediate, "outbox_record", write)

    def open_rows(self) -> list[OutboxRow]:
        """Every open row (the turns that may still write a proactive message)."""
        with self._session("outbox_open_rows") as session:
            stmt = self._items().where(SqlOutboxItem.open.is_(True))
            return [_row(r) for r in session.execute(stmt).scalars().all()]

    def open_for(self, conversation_id: str) -> OutboxRow | None:
        """The Conversation's open row, if any (another replica may have opened it)."""
        with self._session("outbox_open_for") as session:
            stmt = self._items().where(
                SqlOutboxItem.open.is_(True), SqlOutboxItem.conversation_id == conversation_id
            )
            found = session.execute(stmt.limit(1)).scalars().first()
            return _row(found) if found is not None else None

    def bind(
        self,
        row_id: str,
        *,
        item_id: str,
        response_id: str,
        title: str,
        line: str,
        at_ms: int | None,
    ) -> OutboxRow | None:
        """Point the row at the message *item_id* of turn *response_id*; *at_ms* stamps its time
        (the first message), ``None`` keeps it (a later message of the same turn)."""

        def write(session: Session) -> OutboxRow | None:
            row = session.get(SqlOutboxItem, (current_workspace_id(), row_id))
            if row is None:
                return None
            row.item_id = item_id
            row.response_id = response_id
            row.title = title
            row.line = line
            if at_ms is not None:
                row.created_at_ms = at_ms
            session.flush()
            return _row(row)

        return run_write_transaction(self._session_immediate, "outbox_bind", write)

    def close(self, row_id: str) -> None:
        """The row's turn is over: drop it when it never got a message, else keep it closed."""

        def write(session: Session) -> None:
            row = session.get(SqlOutboxItem, (current_workspace_id(), row_id))
            if row is None:
                return
            if row.item_id is None:
                session.delete(row)
            else:
                row.open = False

        run_write_transaction(self._session_immediate, "outbox_close", write)

    def list_after(self, owner: str, after: str | None, *, limit: int = 50) -> list[OutboxRow]:
        """The owner's proactive messages after the message *after* (all when ``None``, or when
        it is not the owner's), oldest first; at most *limit*, the newest ones."""
        with self._session("outbox_list") as session:
            stmt = self._items().where(
                SqlOutboxItem.owner == owner, SqlOutboxItem.item_id.is_not(None)
            )
            since = self._time_of(session, owner, after) if after else None
            if since is not None:
                stmt = stmt.where(SqlOutboxItem.created_at_ms > since)
            stmt = stmt.order_by(
                SqlOutboxItem.created_at_ms.desc(), SqlOutboxItem.id.desc()
            ).limit(limit)
            return [_row(r) for r in reversed(session.execute(stmt).scalars().all())]

    def by_item(self, owner: str, item_id: str) -> OutboxRow | None:
        """The owner's row for the proactive message *item_id*, if it is one."""
        with self._session("outbox_by_item") as session:
            stmt = self._items().where(
                SqlOutboxItem.owner == owner, SqlOutboxItem.item_id == item_id
            )
            found = session.execute(stmt.limit(1)).scalars().first()
            return _row(found) if found is not None else None

    def proactive_between(self, owner: str, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        """The owner's proactive messages created in ``[since_ms, until_ms)``, oldest first, as
        the rhythm reads them (``{item_id, kind, topic_key, created_at_ms}``)."""
        with self._session("outbox_between") as session:
            stmt = (
                self._items()
                .where(
                    SqlOutboxItem.owner == owner,
                    SqlOutboxItem.item_id.is_not(None),
                    SqlOutboxItem.created_at_ms >= since_ms,
                    SqlOutboxItem.created_at_ms < until_ms,
                )
                .order_by(SqlOutboxItem.created_at_ms, SqlOutboxItem.id)
            )
            return [_brief(_row(r)) for r in session.execute(stmt).scalars().all()]

    def proactive_one(self, owner: str, item_id: str) -> dict[str, Any] | None:
        """The owner's proactive message *item_id* as :meth:`proactive_between` serves it."""
        if not _is_item_id(item_id):
            return None
        found = self.by_item(owner, item_id)
        return _brief(found) if found is not None else None

    def labels(self, conversation_id: str, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        """``item id -> proactive label`` for those of *item_ids* that are proactive messages
        (ids that are not item ids, such as a card's, are skipped)."""
        item_ids = [i for i in item_ids if _is_item_id(i)]
        if not item_ids:
            return {}
        with self._session("outbox_labels") as session:
            stmt = self._items().where(
                SqlOutboxItem.conversation_id == conversation_id,
                SqlOutboxItem.item_id.in_(item_ids),
            )
            return {str(r.item_id): _row(r).label() for r in session.execute(stmt).scalars().all()}

    def delete_conversation(self, conversation_id: str) -> None:
        """Drop the rows of a deleted Conversation (cursors and feedback stay the owner's)."""

        def write(session: Session) -> None:
            session.execute(
                delete(SqlOutboxItem).where(
                    SqlOutboxItem.workspace_id == current_workspace_id(),
                    SqlOutboxItem.conversation_id == conversation_id,
                )
            )

        run_write_transaction(self._session_immediate, "outbox_delete_conversation", write)

    @staticmethod
    def _time_of(session: Session, owner: str, item_id: str) -> int | None:
        stmt = select(SqlOutboxItem.created_at_ms).where(
            SqlOutboxItem.workspace_id == current_workspace_id(),
            SqlOutboxItem.owner == owner,
            SqlOutboxItem.item_id == item_id,
        )
        return session.execute(stmt.limit(1)).scalar()

    # ── cursors ─────────────────────────────────────────────────────────────

    def cursor(self, channel: str, owner: str) -> tuple[str, int] | None:
        """``(item id, row time)`` the channel got to for the owner, or ``None`` before any."""
        with self._session("outbox_cursor") as session:
            row = session.get(SqlOutboxCursor, (current_workspace_id(), channel, owner))
            return (row.cursor, row.cursor_ms) if row is not None else None

    def advance(self, channel: str, owner: str, item_id: str) -> bool:
        """Move the channel's cursor to the owner's message *item_id*, never back.

        :returns: ``False`` when *item_id* is not one of the owner's proactive messages.
        """

        def write(session: Session) -> bool:
            at = self._time_of(session, owner, item_id)
            if at is None:
                return False
            key = (current_workspace_id(), channel, owner)
            row = session.get(SqlOutboxCursor, key)
            if row is None:
                session.add(
                    SqlOutboxCursor(channel=channel, owner=owner, cursor=item_id, cursor_ms=at)
                )
            elif at >= row.cursor_ms:
                row.cursor, row.cursor_ms = item_id, at
            return True

        return run_write_transaction(self._session_immediate, "outbox_advance", write)

    def unread(self, owner: str) -> dict[str, Any]:
        """``{count, first_item_id, last_seen_item_id}``: the owner's proactive messages after
        the ``web`` cursor."""
        seen = self.cursor(WEB_CHANNEL, owner)
        with self._session("outbox_unread") as session:
            stmt = self._items().where(
                SqlOutboxItem.owner == owner, SqlOutboxItem.item_id.is_not(None)
            )
            if seen is not None:
                stmt = stmt.where(SqlOutboxItem.created_at_ms > seen[1])
            rows = session.execute(
                stmt.order_by(SqlOutboxItem.created_at_ms, SqlOutboxItem.id)
            ).scalars()
            ids = [str(r.item_id) for r in rows]
        return {
            "count": len(ids),
            "first_item_id": ids[0] if ids else None,
            "last_seen_item_id": seen[0] if seen else None,
        }

    def unseen(self, owner: str, item_id: str) -> bool:
        """Whether *item_id* is one of the owner's proactive messages after the ``web`` cursor."""
        row = self.by_item(owner, item_id) if _is_item_id(item_id) else None
        if row is None:
            return False
        seen = self.cursor(WEB_CHANNEL, owner)
        return seen is None or row.created_at_ms > seen[1]

    # ── feedback ────────────────────────────────────────────────────────────

    def not_useful(self, owner: str, item_id: str, reason: str | None) -> OutboxRow | None:
        """Record *Not useful* on the owner's proactive message (a repeat replaces it).

        :returns: The message's row, or ``None`` when it is not one of the owner's.
        """
        found = self.by_item(owner, item_id)
        if found is None:
            return None
        text = " ".join(reason.split())[:_REASON_MAX] if reason and reason.strip() else None

        def write(session: Session) -> None:
            key = (current_workspace_id(), owner, item_id)
            row = session.get(SqlOutboxFeedback, key)
            if row is None:
                row = SqlOutboxFeedback(owner=owner, item_id=item_id)
                session.add(row)
            row.topic_key = found.topic_key
            row.reason = text
            row.at = now_epoch()

        run_write_transaction(self._session_immediate, "outbox_not_useful", write)
        return found

    def list_mutes(self, owner: str) -> list[dict[str, str]]:
        """The mutes the owner's *Not useful* presses stand for, oldest first, de-duplicated:
        ``{topic_key, sender?, term?}``. A message whose source named a sender or term mutes
        that on its topic; one that named neither mutes the topic. Feedback on a message with no
        topic mutes nothing.
        """
        with self._session("outbox_mutes") as session:
            stmt = (
                select(SqlOutboxFeedback, SqlOutboxItem.source)
                .outerjoin(
                    SqlOutboxItem,
                    (SqlOutboxItem.workspace_id == SqlOutboxFeedback.workspace_id)
                    & (SqlOutboxItem.item_id == SqlOutboxFeedback.item_id)
                    & (SqlOutboxItem.owner == SqlOutboxFeedback.owner),
                )
                .where(
                    SqlOutboxFeedback.workspace_id == current_workspace_id(),
                    SqlOutboxFeedback.owner == owner,
                    SqlOutboxFeedback.topic_key.is_not(None),
                )
                .order_by(SqlOutboxFeedback.at)
            )
            mutes: list[dict[str, str]] = []
            for feedback, raw_source in session.execute(stmt).all():
                try:
                    source = json.loads(raw_source) if raw_source else {}
                except ValueError:
                    source = {}
                mute = {"topic_key": str(feedback.topic_key)}
                for key in ("sender", "term"):
                    if isinstance(source, dict) and isinstance(source.get(key), str):
                        mute[key] = source[key]
                if mute not in mutes:
                    mutes.append(mute)
            return mutes
