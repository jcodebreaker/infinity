"""Topics (ADR 0035): the owner's topics, picking one up, and the writer endpoint.

* ``GET /v1/me/topics?days=7`` lists the owner's open topics touched within ``days`` (at most
  7), newest first; closed ones are hidden.
* ``POST /v1/me/topics/{key}/pick`` appends one ``topic`` card to the owner's Conversation:
  no turn starts and nothing is sent to the model now; the card is part of the record, and the
  next turn gets a one-line note (``note.py``).
* ``POST /v1/topics/write`` is what the Helper's ``topics_update`` tool calls: it validates,
  trims and upserts by key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.superchat.feature import (
    RECORDED_RESPONSE_PREFIX,
    is_super_chat,
    proactive_unseen,
    topic_touched,
)
from omnigent.superchat.topics.note import (
    ACTIVE_LABEL_KEY,
    PICKED_LABEL_KEY,
    active_key,
    active_value,
    conversation_refusal,
    picked_value,
)
from omnigent.superchat.topics.store import SqlAlchemyTopicStore
from omnigent.superchat.topics.topic import WINDOW_DAYS, Topic, clean_key, fenced_body

_logger = logging.getLogger(__name__)

#: The kind of the card a pick appends; drawn by the client, never a ``render_card`` kind the
#: model is offered.
TOPIC_CARD = "topic"
#: The call a pick's card is recorded as, so the transcript draws it like any reply card.
_CARD_CALL = "render_card"


def _zone(timezone: str | None) -> ZoneInfo:
    """The owner's IANA zone, UTC when empty or unknown."""
    try:
        return ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


class WriteTopicsBody(BaseModel):
    """Body of ``POST /v1/topics/write``."""

    model_config = ConfigDict(extra="forbid")

    parent_session_id: str
    topics: list[Any] = Field(default_factory=list)
    #: The calling session: the Conversation itself may write only its active topic. A Helper
    #: (the notes passes) writes any; an older runner, which offers the tool to Helpers only,
    #: leaves it out.
    session_id: str | None = None


class PickBody(BaseModel):
    """Body of ``POST /v1/me/topics/{key}/pick``."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    #: The proactive message the pick came from (its *Open topic*), for the rhythm (ADR 0038).
    item_id: str | None = Field(default=None, max_length=64)


def topic_to_response(topic: Topic, *, prepared: bool = False) -> dict[str, Any]:
    """Serialize a topic for the REST API (``watch`` is the watch's own, not shown).

    :param prepared: Whether the Lookout's prepared draft is still unopened; only then is
        ``prepared`` served (``{item_id, artifact_id, title, line, at}``), else ``None``.
    """
    return {
        "key": topic.key,
        "title": topic.title,
        "left_off": topic.left_off,
        "covered": topic.covered,
        "open": topic.open,
        "starters": topic.starters,
        "events": topic.events,
        "sources": topic.sources,
        "status": topic.status,
        "since": topic.since or None,
        "since_at": topic.since_at,
        "tag": topic.tag or None,
        "status_line": topic.status_line or None,
        "prepared": topic.prepared if prepared and topic.prepared else None,
        "worked_at": topic.worked_at or None,
        "last_touched_at": topic.last_touched_at,
        "updated_at": topic.updated_at,
    }


def _fallback(topic: Topic) -> str:
    """The card as markdown: what the model reads in the record, and any client without it.

    Every line the quiet pass's model wrote is fenced as data.
    """
    title = " ".join(topic.title.replace("[", "(").replace("]", ")").split())
    body = fenced_body(
        left_off=topic.left_off, covered=topic.covered, open_=topic.open, since=topic.since
    )
    return "\n".join([f"Picked up **{title}**.", *([body] if body else [])])


def _current_draft(topic: Topic) -> dict[str, str] | None:
    """The Lookout's draft while nothing moved the topic since it came (``{artifact_id,
    title}``), else ``None``: a writer that took the draft up wrote where the topic stands
    after it, and a newer Lookout message replaced it."""
    prepared = topic.prepared
    if not prepared.get("artifact_id") or int(prepared.get("at") or 0) < topic.left_off_at:
        return None
    return {"artifact_id": str(prepared["artifact_id"]), "title": str(prepared.get("title") or "")}


def topic_card(topic: Topic) -> dict[str, Any]:
    """The ``topic`` card a pick appends, in the shape ``render_card`` returns."""
    return {
        "type": "card",
        "card": TOPIC_CARD,
        "id": f"topic-{topic.key}-{uuid.uuid4().hex[:8]}",
        "title": topic.title,
        "data": {
            "key": topic.key,
            "title": topic.title,
            "left_off": topic.left_off,
            "covered": topic.covered,
            "open": topic.open,
            "starters": topic.starters,
            "sources": topic.sources,
            "since": topic.since or None,
            "since_at": topic.since_at,
            "last_touched_at": topic.last_touched_at,
            "draft": _current_draft(topic),
        },
        "fallback": _fallback(topic),
    }


def append_topic_card(
    conversation_store: ConversationStore, conversation_id: str, card: dict[str, Any]
) -> None:
    """Blocking: record *card* in the Conversation as a finished card call and publish it.

    Stored as a ``render_card`` call with its result, so the transcript draws it like any reply
    card and a session assembled from the record reads the card's fallback. No turn starts, and
    its ``response_id`` (:data:`RECORDED_RESPONSE_PREFIX`) keeps it out of the Activity feed.
    """
    from omnigent.entities import (
        FunctionCallData,
        FunctionCallOutputData,
        NewConversationItem,
    )
    from omnigent.server.routes._sessions.helpers import _publish_external_conversation_item

    response_id = f"{RECORDED_RESPONSE_PREFIX}topic_{uuid.uuid4().hex}"
    call_id = f"call_topic_{uuid.uuid4().hex[:24]}"
    arguments = {k: card[k] for k in ("card", "id", "title", "data", "fallback")}
    persisted = conversation_store.append(
        conversation_id,
        [
            NewConversationItem(
                type="function_call",
                response_id=response_id,
                data=FunctionCallData(
                    agent="superchat",
                    name=_CARD_CALL,
                    arguments=json.dumps(arguments, ensure_ascii=False),
                    call_id=call_id,
                ),
            ),
            NewConversationItem(
                type="function_call_output",
                response_id=response_id,
                data=FunctionCallOutputData(
                    call_id=call_id, output=json.dumps(card, ensure_ascii=False)
                ),
            ),
        ],
    )
    for item in persisted:
        _publish_external_conversation_item(conversation_id, item)


def create_topics_router(
    store: SqlAlchemyTopicStore,
    *,
    scheduled_task_store: Any,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the topics router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    def _owner(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    async def _require_chat(owner: str, session_id: str) -> None:
        try:
            uuid_to_bytes(session_id)
        except InvalidUuidError as exc:
            raise OmnigentError("invalid session id", code=ErrorCode.INVALID_INPUT) from exc
        await require_access(owner, session_id, LEVEL_OWNER, permission_store, conversation_store)

    async def _helper_started(session_id: str, chat_id: str) -> int | None:
        """When *session_id*, a Helper of *chat_id*, started; ``None`` for anything else, which
        is held to the Conversation's rule."""
        caller = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if caller is None or caller.parent_conversation_id != chat_id:
            return None
        return int(caller.created_at)

    def _worked_on(owner: str, key: str, at: int, item_id: str | None = None) -> None:
        """The person worked on topic *key*: Home stops leading with a reason they acted on, and
        the rhythm hears it (blocking; never fails the person's action)."""
        try:
            store.mark_worked(owner, key, at)
        except Exception:
            _logger.warning("topics: could not record work on %s", key, exc_info=True)
        topic_touched(owner, key, at, item_id)

    @router.get("/me/topics")
    async def list_topics(
        request: Request, days: int = Query(default=WINDOW_DAYS, ge=1, le=WINDOW_DAYS)
    ) -> dict[str, Any]:
        """The owner's open topics touched within ``days``, newest first."""
        owner = _owner(request)

        def read() -> list[dict[str, Any]]:
            # A draft the Lookout prepared leads Home only until the person saw its message.
            return [
                topic_to_response(
                    t,
                    prepared=bool(t.prepared.get("item_id"))
                    and proactive_unseen(owner, str(t.prepared["item_id"])),
                )
                for t in store.list_recent(owner, days=days)
            ]

        return {"topics": await asyncio.to_thread(read)}

    @router.post("/me/topics/{key}/pick")
    async def pick_topic(request: Request, key: str, body: PickBody) -> dict[str, Any]:
        """Append the topic's card to the Conversation (no turn starts) and return it."""
        owner = _owner(request)
        await _require_chat(owner, body.conversation_id)
        conv = await asyncio.to_thread(conversation_store.get_conversation, body.conversation_id)
        if (
            conv is None
            or conv.parent_conversation_id is not None
            or not is_super_chat(conv.labels)
        ):
            raise OmnigentError("not a Conversation", code=ErrorCode.INVALID_INPUT)
        topic = await asyncio.to_thread(store.get, owner, clean_key(key))
        if topic is None or topic.status != "open":
            raise OmnigentError("no such topic", code=ErrorCode.NOT_FOUND)
        card = topic_card(topic)
        await asyncio.to_thread(append_topic_card, conversation_store, body.conversation_id, card)
        # A warm session never reads the card: the next turn is told in one line (note.py).
        await asyncio.to_thread(
            conversation_store.set_labels,
            body.conversation_id,
            {
                PICKED_LABEL_KEY: picked_value(topic.key, topic.title),
                # Until another pick or 3 hours: the Conversation keeps this topic current.
                ACTIVE_LABEL_KEY: active_value(topic.key, int(time.time())),
            },
        )
        # The person took the topic up: the rhythm hears it (ADR 0038), never failing the pick.
        await asyncio.to_thread(_worked_on, owner, topic.key, int(time.time()), body.item_id)
        return {"card": card}

    @router.post("/topics/write")
    async def write_topics(request: Request, body: WriteTopicsBody) -> dict[str, Any]:
        """The notes Helper's upsert (cleaned, trimmed, by key), or the Conversation's own
        update of the topic the person picked up."""
        owner = _owner(request)
        await _require_chat(owner, body.parent_session_id)
        active = ""
        # A Helper (a quiet or seed pass) read the Conversation from its start: where a topic
        # stands, written after that (the Lookout posted meanwhile), is newer than its view.
        view_at = (
            await _helper_started(body.session_id, body.parent_session_id)
            if body.session_id is not None
            else None
        )
        if body.session_id is not None and view_at is None:
            conv = await asyncio.to_thread(
                conversation_store.get_conversation, body.parent_session_id
            )
            labels = conv.labels if conv is not None else None
            active = active_key((labels or {}).get(ACTIVE_LABEL_KEY), time.time())
            keys = [clean_key(t.get("key")) if isinstance(t, dict) else "" for t in body.topics]
            if refusal := conversation_refusal(keys, active):
                raise OmnigentError(refusal, code=ErrorCode.INVALID_INPUT)
        prefs = await asyncio.to_thread(scheduled_task_store.get_owner_preferences, owner)
        zone = _zone(prefs.timezone if prefs else None)
        result = await asyncio.to_thread(
            lambda: store.update(owner, body.topics, zone=zone, view_at=view_at)
        )
        written = result["written"]
        if active and any(w.get("key") == active for w in written):
            # The Conversation itself worked on its topic: the person touched it (ADR 0038).
            await asyncio.to_thread(_worked_on, owner, active, int(time.time()))
        return {
            **result,
            "new": sum(1 for w in written if w["created"]),
            "reused": sum(1 for w in written if not w["created"]),
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    return router
