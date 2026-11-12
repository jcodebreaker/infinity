"""Topic store: one row per owner per topic (owner-scoped), upserted by key.

Other capabilities read and mark topics through three functions only,
:func:`list_watched_topics`, :func:`mark_topic_new` and :func:`set_topic_status`, which use the
store the feature installed, and hear every write through :func:`register_topic_listener`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlTopic, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.superchat.topics.topic import (
    MAX_ITEM_CHARS,
    MAX_TOPICS_PER_CALL,
    WINDOW_DAYS,
    Topic,
    clean_status_line,
    clean_update,
    fence,
    merge,
    message_lead,
    topic_lines,
    with_open_ids,
)

_logger = logging.getLogger(__name__)

LOCAL_OWNER = "local"
_DAY = 86400
_UTC = ZoneInfo("UTC")


def _to_entity(row: SqlTopic) -> Topic:
    try:
        body = json.loads(row.body)
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return Topic(
        owner=row.owner,
        key=row.key,
        title=row.title,
        left_off=str(body.get("left_off") or ""),
        covered=list(body.get("covered") or []),
        open=with_open_ids(list(body.get("open") or [])),
        starters=list(body.get("starters") or []),
        events=list(body.get("events") or []),
        sources=dict(body.get("sources") or {}),
        watch=dict(body.get("watch") or {}),
        status=row.status,
        since=str(body.get("since") or ""),
        tag=str(body.get("tag") or ""),
        status_line=str(body.get("status_line") or ""),
        prepared=dict(body.get("prepared") or {}),
        worked_at=int(body.get("worked_at") or 0),
        left_off_at=int(body.get("left_off_at") or 0),
        since_at=row.since_at,
        last_touched_at=row.last_touched_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _write_row(row: SqlTopic, topic: Topic) -> None:
    row.title = topic.title
    row.status = topic.status
    row.body = json.dumps(topic.body(), ensure_ascii=False)
    row.last_touched_at = topic.last_touched_at
    row.since_at = topic.since_at
    row.updated_at = topic.updated_at


class SqlAlchemyTopicStore:
    """SQLAlchemy-backed store for the ``topics`` table."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.topic_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.topic_store", immediate=True
        )

    @staticmethod
    def owner_key(user_id: str | None) -> str:
        """The stored owner key for a user id (``None`` is single-user mode)."""
        return user_id or LOCAL_OWNER

    def get(self, owner: str, key: str) -> Topic | None:
        """The owner's topic, or ``None``."""
        with self._session("select_topic") as session:
            row = session.get(SqlTopic, (current_workspace_id(), owner, key))
            return _to_entity(row) if row else None

    def list_recent(
        self, owner: str, *, days: int = WINDOW_DAYS, now: int | None = None
    ) -> list[Topic]:
        """Open topics touched within *days* (at most :data:`WINDOW_DAYS`), newest first."""
        since = (now if now is not None else now_epoch()) - min(days, WINDOW_DAYS) * _DAY
        with self._session("list_topics") as session:
            stmt = (
                select(SqlTopic)
                .where(
                    SqlTopic.workspace_id == current_workspace_id(),
                    SqlTopic.owner == owner,
                    SqlTopic.status == "open",
                    SqlTopic.last_touched_at >= since,
                )
                .order_by(desc(SqlTopic.last_touched_at))
                .limit(100)
            )
            return [_to_entity(r) for r in session.execute(stmt).scalars().all()]

    def has_any(self, owner: str) -> bool:
        """Whether the owner has ever had a topic (closed and old ones count)."""
        with self._session("any_topic") as session:
            stmt = select(SqlTopic.key).where(
                SqlTopic.workspace_id == current_workspace_id(), SqlTopic.owner == owner
            )
            return session.execute(stmt.limit(1)).first() is not None

    def update(
        self,
        owner: str,
        entries: list[Any],
        *,
        zone: ZoneInfo,
        now: int | None = None,
        view_at: int | None = None,
    ) -> dict[str, Any]:
        """Upsert ``topics_update`` entries by key, after cleaning them (:func:`clean_update`).

        A new topic needs a title; an entry that cannot be used is reported, never repaired.
        *view_at* is when a background writer began reading: where a topic stands, written
        after that, is kept (:func:`merge`).

        :returns: ``{"written": [{key, created}], "skipped": [{key?, reason}]}``.
        """
        instant = now if now is not None else now_epoch()
        skipped: list[dict[str, Any]] = []
        if len(entries) > MAX_TOPICS_PER_CALL:
            skipped.append({"reason": f"only the first {MAX_TOPICS_PER_CALL} topics are kept"})
        cleaned: dict[str, dict[str, Any]] = {}
        for raw in entries[:MAX_TOPICS_PER_CALL]:
            result = clean_update(raw, zone, instant)
            if isinstance(result, str):
                key = raw.get("key") if isinstance(raw, dict) else None
                skipped.append(
                    {**({"key": key} if isinstance(key, str) else {}), "reason": result}
                )
                continue
            key, fields = result
            cleaned[key] = {**cleaned.get(key, {}), **fields}

        def write(session: Session) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list]:
            # Built fresh on every attempt: the transaction may be replayed.
            done: list[dict[str, Any]] = []
            refused: list[dict[str, Any]] = []
            changes: list[tuple[Topic | None, Topic]] = []
            for key, fields in cleaned.items():
                row = session.get(SqlTopic, (current_workspace_id(), owner, key))
                if row is None:
                    if not fields.get("title"):
                        refused.append({"key": key, "reason": "a new topic needs a title"})
                        continue
                    before = None
                    topic = merge(
                        Topic(owner=owner, key=key, title="", created_at=instant), fields, instant
                    )
                    row = SqlTopic(owner=owner, key=key, created_at=instant)
                    _write_row(row, topic)
                    session.add(row)
                else:
                    before = _to_entity(row)
                    topic = merge(_to_entity(row), fields, instant, view_at=view_at)
                    _write_row(row, topic)
                # The open lines with their settled ids, so the writer can pass them back.
                done.append({"key": key, "created": before is None, "open": topic.open})
                changes.append((before, topic))
            session.flush()
            return done, refused, changes

        written, refused, changes = run_write_transaction(
            self._session_immediate, "topics_update", write
        )
        notify_topic_listeners(owner, changes)
        return {"written": written, "skipped": skipped + refused}

    def mark_new(self, owner: str, key: str, line: str, at: int) -> bool:
        """Set a topic's ``since`` ("New") line; ``False`` when there is no such topic."""
        text = " ".join(str(line).split())[:MAX_ITEM_CHARS]

        def write(session: Session) -> tuple[Topic, Topic] | None:
            row = session.get(SqlTopic, (current_workspace_id(), owner, key))
            if row is None:
                return None
            before, topic = _to_entity(row), _to_entity(row)
            topic.since, topic.since_at, topic.updated_at = text, int(at), now_epoch()
            _write_row(row, topic)
            return before, topic

        change = run_write_transaction(self._session_immediate, "topic_mark_new", write)
        if change is not None:
            notify_topic_listeners(owner, [change])
        return change is not None

    def set_status(
        self,
        owner: str,
        key: str,
        line: str | None,
        prepared: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> bool:
        """After the Lookout posted *message* on a topic: its status line (when *line* is
        given), the message's first sentence as ``left_off`` and as a dated event (so the pick
        card never shows an older summary), and, when *prepared* is given, the draft the
        message carried (``{item_id, artifact_id, title, line, at}``, ``at`` the time of this
        write, so it equals ``left_off_at`` until another writer moves the topic). ``False``
        when there is no such topic."""
        status = clean_status_line(line) if line is not None else None
        kept = _prepared(prepared) if prepared is not None else None
        lead = message_lead(message) if message else ""

        def write(session: Session) -> tuple[Topic, Topic] | None:
            row = session.get(SqlTopic, (current_workspace_id(), owner, key))
            if row is None:
                return None
            at = now_epoch()
            before, topic = _to_entity(row), _to_entity(row)
            fields: dict[str, Any] = {"status_line": status} if status else {}
            if lead:
                # Cleaned like any writer's: the event line is cut to an event's length.
                cleaned = clean_update(
                    {"key": key, "left_off": lead, "events": [{"at": at, "line": lead}]},
                    _UTC,
                    at,
                )
                fields |= cleaned[1] if isinstance(cleaned, tuple) else {}
            merge(topic, fields, at)
            if kept is not None:
                topic.prepared = {**kept, "at": at} if kept else {}
            topic.updated_at = at
            _write_row(row, topic)
            return before, topic

        change = run_write_transaction(self._session_immediate, "topic_set_status", write)
        if change is not None:
            notify_topic_listeners(owner, [change])
        return change is not None

    def mark_worked(self, owner: str, key: str, at: int) -> bool:
        """Record that the person worked on topic *key* at *at* (a pick, or the Conversation's
        own write of its topic). Nothing a topic listener reads changes, so none is told.
        ``False`` when there is no such topic."""

        def write(session: Session) -> bool:
            row = session.get(SqlTopic, (current_workspace_id(), owner, key))
            if row is None:
                return False
            topic = _to_entity(row)
            topic.worked_at = max(topic.worked_at, int(at))
            _write_row(row, topic)
            return True

        return run_write_transaction(self._session_immediate, "topic_mark_worked", write)


def _prepared(raw: dict[str, Any]) -> dict[str, Any]:
    """The Lookout's prepared-work pointer, trimmed: ids and short strings only."""
    out: dict[str, Any] = {}
    for name in ("item_id", "artifact_id", "title", "line"):
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = " ".join(value.split())[: MAX_ITEM_CHARS * 2 if name == "line" else 200]
    at = raw.get("at")
    if isinstance(at, int) and not isinstance(at, bool):
        out["at"] = at
    return out if out.get("item_id") else {}


#: Called after every topic write, all writers (ADR 0038): ``fn(owner, before, after)``.
_listeners: list[Callable[[str, Topic | None, Topic], None]] = []


def register_topic_listener(fn: Callable[[str, Topic | None, Topic], None]) -> None:
    """Call ``fn(owner, before, after)`` after every committed topic write, for each topic it
    wrote (``before`` is ``None`` for a new topic). Called on the writer's thread, never on the
    event loop; an exception is logged and never fails the write. Registering twice is a no-op.
    """
    if fn not in _listeners:
        _listeners.append(fn)


def unregister_topic_listener(fn: Callable[[str, Topic | None, Topic], None]) -> None:
    """Stop calling *fn* (tests)."""
    if fn in _listeners:
        _listeners.remove(fn)


def notify_topic_listeners(owner: str, changes: list[tuple[Topic | None, Topic]]) -> None:
    """Tell every listener about *changes*; one failing listener never stops the others."""
    for before, after in changes:
        for fn in list(_listeners):
            try:
                fn(owner, before, after)
            except Exception:
                _logger.exception("topic listener failed", extra={"topic": after.key})


#: The store :func:`install` set (the feature's ``install``); ``None`` before it ran.
_store: SqlAlchemyTopicStore | None = None


def install(store: SqlAlchemyTopicStore | None) -> None:
    """Set the store the module functions below use."""
    global _store
    _store = store


def list_watched_topics(owner: str) -> list[dict[str, Any]]:
    """Open topics touched within 7 days, for the Lookout and the daily brief: key, title,
    left_off, open, watch, last_touched_at and the "New" line (since, since_at). Empty before the
    feature is installed."""
    if _store is None:
        return []
    return [
        {
            "key": t.key,
            "title": t.title,
            "left_off": t.left_off,
            "open": t.open,
            "watch": t.watch,
            "last_touched_at": t.last_touched_at,
            "since": t.since,
            "since_at": t.since_at,
            "tag": t.tag,
            "status_line": t.status_line,
        }
        for t in _store.list_recent(owner)
    ]


def mark_topic_new(owner: str, key: str, line: str, at: int) -> bool:
    """Set topic *key*'s "New" line (shown on Home for 48 hours); ``False`` when not found."""
    return _store.mark_new(owner, key, line, at) if _store is not None else False


def set_topic_status(
    owner: str,
    key: str,
    line: str | None,
    prepared: dict[str, Any] | None = None,
    message: str | None = None,
) -> bool:
    """After the Lookout posted *message* on topic *key*: its status line, where it stands and,
    optionally, the prepared draft (:meth:`SqlAlchemyTopicStore.set_status`); ``False`` when
    not found."""
    return _store.set_status(owner, key, line, prepared, message) if _store is not None else False


def brief_lines(owner: str) -> str:
    """The owner's topics touched this week, whole (where each stands, its covered lines and its
    open lines with their ids), fenced as data, for the quiet brief; ``""`` when there are none.

    The writer sees what it replaces: ``covered`` and ``open`` are replaced whole, so a pass that
    saw only the title would drop lines another writer added (ADR 0038, amended).
    """
    lines = [
        line
        for topic in (_store.list_recent(owner) if _store is not None else [])
        for line in topic_lines(topic)
    ]
    return fence("\n".join(lines)) if lines else ""
