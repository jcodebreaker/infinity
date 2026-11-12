"""Where a proactive message gets its label and its outbox row (ADR 0036).

A scheduled run's Result reaches the Conversation as a notice posted with ``context.helper_id``
(the runner's wake). That notice, stored while no turn runs, is the one place the next turn's
origin is known: :meth:`Outbox.notice` opens a row for it then, from the Helper's scheduled-task
label (the kind) and its ``omnigent.proactive`` label (urgency, topic, what matched). The turn
it starts writes its messages; :meth:`Outbox.message` binds the row to the first one (once the
item history confirms the notice is the message the turn answers) and moves it to each later
message of the same turn, whose last text is its reply. A message of another turn closes the
row. The label lives on the row, keyed by the message, and is read back onto the transcript.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from typing import Any

from omnigent.context.labels import PROACTIVE_LABEL_KEY, SCHEDULED_HELPER_LABEL_KEY
from omnigent.context.rollover import _MID_TURN_LIVE_STATUSES
from omnigent.entities import ConversationItem, MessageData
from omnigent.evals.identity import is_eval_identity
from omnigent.superchat.feature import RECORDED_RESPONSE_PREFIX, is_super_chat
from omnigent.superchat.outbox.cut import (
    DEFAULT_KIND,
    DEFAULT_URGENCY,
    KIND_URGENCY,
    cut,
    message_text,
    parse_label,
)
from omnigent.superchat.outbox.store import OutboxRow, SqlAlchemyOutboxStore, owner_key

_logger = logging.getLogger(__name__)

#: How far back from a turn's first message the notice that started it is looked for.
_LOOKBACK_PAGE = 50
_LOOKBACK_PAGES = 4


class Outbox:
    """Opens, binds and closes outbox rows as the Conversation's messages are stored."""

    def __init__(
        self,
        store: SqlAlchemyOutboxStore,
        conversation_store: Any,
        scheduled_task_store: Any,
    ) -> None:
        self._store = store
        self._conversations = conversation_store
        self._tasks = scheduled_task_store
        self._lock = threading.Lock()
        #: ``conversation id -> its open row``: only these Conversations' messages are looked at.
        self._open: dict[str, OutboxRow] = {row.conversation_id: row for row in store.open_rows()}

    def notice(self, session_id: str, item_id: str, context: Mapping[str, Any]) -> None:
        """A message carrying *context* was stored in *session_id*: when it is a proactive
        source's notice and no turn runs, the turn it starts is proactive (blocking).

        Two sources: a scheduled Helper's Result (``context.helper_id``; the kind is its task's,
        the rest its ``omnigent.proactive`` label), and engine code that wakes the Conversation
        itself (``context.proactive``: ``{kind, topic_key?, urgency?,
        sender?, term?}``).
        """
        label = self._source_label(session_id, context)
        if label is None:
            return
        parent = self._conversations.get_conversation(session_id)
        if parent is None or not is_super_chat(parent.labels):
            return
        if parent.live_status in _MID_TURN_LIVE_STATUSES:
            # Steered into the person's running turn: that turn is theirs, not the run's.
            return
        owner = self._conversations.get_session_owner(session_id)
        if is_eval_identity(owner):
            return  # an eval person gets no proactive messages (ADR 0042)
        row = self._store.open_row(
            OutboxRow(
                id=item_id,
                owner=owner_key(owner),
                conversation_id=session_id,
                kind=label["kind"] or DEFAULT_KIND,
                urgency=label["urgency"],
                topic_key=label["topic_key"],
                source=label["source"],
                created_at_ms=int(time.time() * 1000),
            )
        )
        with self._lock:
            self._open[session_id] = row

    def _source_label(self, session_id: str, context: Mapping[str, Any]) -> dict[str, Any] | None:
        """The proactive label *context* names, or ``None`` when it is from no proactive
        source."""
        direct = context.get("proactive")
        if isinstance(direct, Mapping):
            return parse_label(direct)
        helper_id = context.get("helper_id")
        if not isinstance(helper_id, str) or not helper_id:
            return None
        helper = self._conversations.get_conversation(helper_id)
        labels = (helper.labels if helper is not None else None) or {}
        task_id = labels.get(SCHEDULED_HELPER_LABEL_KEY)
        if not task_id or helper.parent_conversation_id != session_id:
            return None
        task = self._tasks.get(task_id) if self._tasks is not None else None
        kind = getattr(task, "kind", None)
        kind = kind[:32] if isinstance(kind, str) and kind else None
        label = parse_label(
            labels.get(PROACTIVE_LABEL_KEY),
            default_urgency=KIND_URGENCY.get(kind or "", DEFAULT_URGENCY),
        )
        label["kind"] = kind
        return label

    def message(self, session_id: str, item: ConversationItem) -> None:
        """An assistant message was stored in *session_id*: label it when its turn is the open
        row's (blocking: the caller runs it off the event loop).

        The in-memory rows are this replica's; a Conversation whose row another replica opened
        (or replaced) is read from the store, the one place every replica's rows are.
        """
        if not isinstance(item.data, MessageData):
            return
        if item.response_id.startswith(RECORDED_RESPONSE_PREFIX):
            # Posted by engine code with its own row (:meth:`record`): no turn's message.
            return
        with self._lock:
            cached = self._open.get(session_id)
        row = cached or self._store.open_for(session_id)
        if row is None:
            return
        bound = self._settle(session_id, item, row)
        if bound is None and cached is not None:
            fresh = self._store.open_for(session_id)
            if fresh is not None and fresh.id != row.id:
                bound = self._settle(session_id, item, fresh)
        with self._lock:
            if bound is None:
                self._open.pop(session_id, None)
            else:
                self._open[session_id] = bound

    def _settle(self, session_id: str, item: ConversationItem, row: OutboxRow) -> OutboxRow | None:
        """Bind *row* to *item* when it is the row's turn's message, else close it.

        :returns: The bound row, or ``None`` when the row was closed.
        """
        assert isinstance(item.data, MessageData)
        if row.item_id is None and self._answers(session_id, item.id, row.id):
            at_ms: int | None = int(time.time() * 1000)
        elif row.item_id is not None and row.response_id == item.response_id:
            at_ms = None
        else:
            self._store.close(row.id)
            return None
        title, line = cut(message_text(item.data.content))
        return self._store.bind(
            row.id,
            item_id=item.id,
            response_id=item.response_id,
            title=title,
            line=line,
            at_ms=at_ms,
        )

    def record(
        self,
        *,
        owner: str,
        conversation_id: str,
        item_id: str,
        response_id: str,
        text: str,
        kind: str,
        urgency: str,
        topic_key: str | None = None,
        source: Mapping[str, str] | None = None,
    ) -> None:
        """A proactive message engine code posted itself (ADR 0038): its row, bound and closed
        (blocking). The label is parsed like a source's, so a bad value is dropped."""
        if is_eval_identity(owner):
            return
        label = parse_label(
            {"kind": kind, "urgency": urgency, "topic_key": topic_key, **(source or {})}
        )
        title, line = cut(text)
        self._store.record(
            OutboxRow(
                id=item_id,
                owner=owner_key(owner),
                conversation_id=conversation_id,
                kind=label["kind"] or DEFAULT_KIND,
                urgency=label["urgency"],
                topic_key=label["topic_key"],
                source=label["source"],
                created_at_ms=int(time.time() * 1000),
                item_id=item_id,
                response_id=response_id,
                title=title,
                line=line,
                open=False,
            )
        )

    def forget(self, session_id: str) -> None:
        """A Conversation was deleted: drop its rows."""
        with self._lock:
            self._open.pop(session_id, None)
        self._store.delete_conversation(session_id)

    def _answers(self, session_id: str, item_id: str, notice_id: str) -> bool:
        """Whether the user-role message nearest before *item_id* is the notice *notice_id*:
        the message's turn is the one the notice started, not one the person started since."""
        cursor = item_id
        for _ in range(_LOOKBACK_PAGES):
            page = self._conversations.list_items(
                session_id, limit=_LOOKBACK_PAGE, after=cursor, order="desc"
            )
            for earlier in page.data:
                if (
                    earlier.type == "message"
                    and isinstance(earlier.data, MessageData)
                    and earlier.data.role == "user"
                    and not earlier.data.is_meta
                ):
                    return earlier.id == notice_id
            if not page.has_more or not page.data:
                return False
            cursor = page.data[-1].id
        return False
