"""Which artifact a person's message is about, and which message a version came from.

docs/conversation.md (ADR 0021). A message gets its link three ways: the artifact open beside
the chat when it was sent (``open``, from the message event's ``context``), the first save
during its turn (``edited``), or the person's pick on the chip (``chosen``). A version records
the person's message in the Conversation that led to it (``item_id``). The links live in the
artifact store (``message_artifacts``); the transcript reads them onto the person's messages,
and a change is announced on the family stream so the web redraws the chip. An assistant message
engine code posted (ADR 0038) is linked ``made`` to the file its run saved, and shows it as the
file's card.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.entities import MessageData
from omnigent.entities.conversation import is_system_notice
from omnigent.stores import ConversationStore
from omnigent.superchat.artifact_kinds import KIND_MIME
from omnigent.superchat.artifacts.store import (
    LINK_EDITED,
    LINK_MADE,
    LINK_OPEN,
    Artifact,
    MessageLink,
    SqlAlchemyArtifactStore,
)
from omnigent.superchat.family.signals import notify_item_updated

_logger = logging.getLogger(__name__)

#: How many of the Conversation's newest items are searched for the call that started a Helper.
_LOOKBACK = 200
#: Most chats walked up from a Helper to the Conversation (a Helper's own Helper is two).
_MAX_DEPTH = 3


def _is_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid_to_bytes(value)
    except InvalidUuidError:
        return False
    return True


def parse_open_artifact(context: Mapping[str, Any] | None) -> str | None:
    """The id of the artifact open beside the chat, from a message event's ``context``.

    ``{"open_artifact": {"artifact_id": "<32 hex>", "version": 4}}``; anything else (no context,
    another shape, a malformed id) is no open artifact. The version names what the person saw;
    the link names the artifact, and the version it produced is filled in by a save.
    """
    if not isinstance(context, Mapping):
        return None
    opened = context.get("open_artifact")
    if not isinstance(opened, Mapping):
        return None
    artifact_id = opened.get("artifact_id")
    return artifact_id if _is_id(artifact_id) else None


def is_person_message(item: Any) -> bool:
    """Whether a stored item is a message the person typed (not a runtime notice)."""
    data = getattr(item, "data", None)
    return (
        getattr(item, "type", None) == "message"
        and isinstance(data, MessageData)
        and data.role == "user"
        and not data.is_meta
        and not is_system_notice(data)
    )


def next_link(current: MessageLink | None, saved: Artifact) -> MessageLink | None:
    """The message's link once *saved* was saved during its turn, or ``None`` to keep it.

    No link yet, or an ``open`` link to another artifact: the save names the artifact
    (``edited``). A link to the same artifact (``open``, ``edited`` or ``chosen``) gains the
    version the message produced. A link to another artifact that the save did not come from
    (an earlier ``edited`` one, or one the person ``chosen``) is kept, and so is "none".
    """
    if current is None or (current.how == LINK_OPEN and current.name != saved.name):
        return MessageLink(saved.id, saved.name, saved.version, LINK_EDITED)
    if current.artifact_id is None or current.name != saved.name:
        return None
    if current.version == saved.version:
        return None
    artifact_id = saved.id if current.how == LINK_EDITED else current.artifact_id
    return MessageLink(artifact_id, current.name, saved.version, current.how)


def record_open_artifact(
    store: SqlAlchemyArtifactStore, session_id: str, item_id: str, context: Mapping[str, Any]
) -> MessageLink | None:
    """Link a just-stored message to the artifact open beside the chat (``how: "open"``).

    An artifact that is not this Conversation's is ignored: the context is a hint from the web.
    """
    artifact_id = parse_open_artifact(context)
    if artifact_id is None:
        return None
    opened = store.get_in_session(artifact_id, session_id)
    if opened is None:
        return None
    link = MessageLink(opened.id, opened.name, None, LINK_OPEN)
    store.set_message_link(session_id, item_id, link)
    return link


def link_saved_version(store: SqlAlchemyArtifactStore, saved: Artifact) -> MessageLink | None:
    """Fill the link of the message *saved* came from, and tell the family stream.

    :returns: The new link, or ``None`` when the version has no message or the link is kept.
    """
    if saved.item_id is None:
        return None
    session_id = saved.parent_session_id
    link = next_link(store.message_link(session_id, saved.item_id), saved)
    if link is None:
        return None
    store.set_message_link(session_id, saved.item_id, link)
    notify_item_updated(session_id, saved.item_id)
    return link


def attach_links(
    store: SqlAlchemyArtifactStore, session_id: str, messages: list[dict[str, Any]]
) -> None:
    """Put each linked person's message's ``artifact`` field on transcript *messages*, and the
    file card of each code-posted assistant message's ``made`` link.

    A message with no link has no field; one the person said is about nothing has ``None``.
    """
    ids = [
        m["id"]
        for m in messages
        if m.get("role") in ("user", "assistant") and isinstance(m.get("id"), str)
    ]
    links = store.message_links(session_id, ids)
    for message in messages:
        link = links.get(message.get("id"))  # type: ignore[arg-type]
        if link is None:
            continue
        if message.get("role") == "user":
            message["artifact"] = link.to_wire()
        elif link.how == LINK_MADE and link.artifact_id is not None:
            made = store.get_in_session(link.artifact_id, session_id)
            if made is not None and isinstance(message.get("blocks"), list):
                message["blocks"].append(made_file_block(made))


def made_file_block(saved: Artifact) -> dict[str, Any]:
    """The transcript's ``file`` block of *saved* (the shape an ``artifact_save`` card has)."""
    block: dict[str, Any] = {
        "type": "file",
        "artifact_id": saved.id,
        "name": saved.name,
        "mime": KIND_MIME.get(saved.kind),
        "kind": saved.kind,
        "size": saved.size,
        "version": saved.version,
    }
    if saved.title:
        block["title"] = saved.title
    return block


def link_made(
    store: SqlAlchemyArtifactStore, session_id: str, item_id: str, artifact_id: str
) -> bool:
    """Link the assistant message *item_id* to the file *artifact_id* its run saved, when that
    file belongs to the Conversation *session_id*.

    :returns: Whether the link was written.
    """
    saved = store.get_in_session(artifact_id, session_id)
    if saved is None:
        return False
    store.set_message_link(
        session_id, item_id, MessageLink(saved.id, saved.name, saved.version, LINK_MADE)
    )
    return True


def open_artifact(
    store: SqlAlchemyArtifactStore, session_id: str, item_id: str | None
) -> Artifact | None:
    """The newest version of the artifact the message *item_id* was sent with open, if any."""
    if item_id is None:
        return None
    link = store.message_link(session_id, item_id)
    if link is None or link.how != LINK_OPEN or link.artifact_id is None:
        return None
    opened = store.get_in_session(link.artifact_id, session_id)
    return store.newest(opened) if opened is not None else None


def conversation_item(
    conversations: ConversationStore,
    parent_session_id: str,
    chat_id: str | None,
    turn_item_id: str | None,
) -> str | None:
    """The person's message in the Conversation a version saved by *chat_id* came from.

    The Conversation's own save: its turn's message. A Helper's save (or its own Helper's): the
    person's message before the call that started the Helper under the Conversation. A
    Helper no call started (a scheduled or ad-hoc run), any other chat, or a failed lookup has
    none.
    """
    if chat_id is None:
        return None
    if chat_id == parent_session_id:
        return turn_item_id
    try:
        return _helper_origin(conversations, parent_session_id, chat_id)
    except Exception:  # noqa: BLE001 - a label, never a reason to lose a version
        _logger.warning("artifacts: could not find the message behind %s", chat_id)
        return None


def _helper_origin(
    conversations: ConversationStore, parent_session_id: str, chat_id: str
) -> str | None:
    chat = conversations.get_conversation(chat_id)
    for _ in range(_MAX_DEPTH):
        if chat is None or chat.parent_conversation_id is None:
            return None
        if chat.parent_conversation_id == parent_session_id:
            break
        chat = conversations.get_conversation(chat.parent_conversation_id)
    else:
        return None
    # The call that started the Helper answered with its id; the person's message before it is
    # the one the Helper came from.
    page = conversations.list_items(parent_session_id, limit=_LOOKBACK, order="desc")
    started = False
    for item in page.data:
        if not started:
            output = getattr(item.data, "output", None)
            started = item.type == "function_call_output" and chat.id in str(output or "")
        elif is_person_message(item):
            return item.id
    return None


STEER_CHOSEN = 'The person means the artifact "{name}" ({artifact_id}).'
STEER_NONE = "The person says this message is not about an artifact."


def steer_text(link: MessageLink) -> str:
    """The one line a running turn is steered with after the person relinks its message."""
    if link.artifact_id is None:
        return STEER_NONE
    return STEER_CHOSEN.format(name=link.name or "", artifact_id=link.artifact_id)
