"""The family tree of a Super Chat: which chats belong to it, and where a Helper stands.

A Super Chat (the chat a person talks in) and the Helpers it started. Every capability that
follows a family (the family event stream, search, the transcript, Activity) builds on these
reads; none of them is owned by a capability.
"""

from __future__ import annotations

import time

from omnigent.context.labels import LAUNCHING_LABEL_KEY, is_superside_chat
from omnigent.entities import Conversation, ConversationItem
from omnigent.stores.conversation_store import ConversationStore
from omnigent.tools.builtins.spawn import _project_activity_item
from omnigent.util.session_lifecycle import is_session_closed

STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: A Helper the server marked as launching that still has not reported any status after this
#: long never started: it reads as failed rather than working forever.
LAUNCH_TIMEOUT_SECONDS = 300

#: Characters of a message read when finding a Helper's last reply.
_MESSAGE_MAX_CHARS = 560


def message_text(item: ConversationItem) -> str | None:
    projected = _project_activity_item(item, max_chars=_MESSAGE_MAX_CHARS)
    text = projected.get("content")
    return text or None


def last_assistant_text(items: list[ConversationItem]) -> str | None:
    for item in reversed(items):
        if item.type == "message" and getattr(item.data, "role", None) == "assistant":
            text = message_text(item)
            if text:
                return text
    return None


def sub_agent_status(
    conversation: Conversation,
    items: list[ConversationItem] | None = None,
    *,
    now: float | None = None,
) -> str:
    """Map a sub-agent conversation's live status (+ close marker) to an Activity status.

    ``live_status`` is authoritative when the runtime has reported one.
    Flagged ambiguity: the store has no distinct "cancelled" marker
    separate from a normal close, so a sub-agent tombstoned before ever
    reporting a turn (``live_status`` still unset) reads as Cancelled;
    one tombstoned after finishing at least one turn reads as Done.

    A Helper the server marked as launching (``LAUNCHING_LABEL_KEY``) that has not reported a
    status yet and has said nothing is *starting*: In progress, so it never shows as Done
    before it has begun. If it still has not reported after ``LAUNCH_TIMEOUT_SECONDS`` it
    never started, which reads as Failed.
    """
    if conversation.live_status == "failed":
        return STATUS_FAILED
    if conversation.live_status in ("running", "waiting"):
        return STATUS_IN_PROGRESS
    if conversation.live_status is None:
        if is_session_closed(conversation.labels, conversation.title):
            return STATUS_CANCELLED
        launching = LAUNCHING_LABEL_KEY in conversation.labels
        if launching and last_assistant_text(items or []) is None:
            age = (time.time() if now is None else now) - conversation.created_at
            return STATUS_IN_PROGRESS if age < LAUNCH_TIMEOUT_SECONDS else STATUS_FAILED
    return STATUS_DONE


def resolve_super_chat_id(conv_store: ConversationStore, session_id: str) -> str | None:
    """Resolve ``session_id`` to its Super Chat id.

    A Super Chat resolves to itself.

    :param conv_store: Store to query.
    :param session_id: A Super Chat conversation id.
    :returns: The Super Chat's conversation id, or ``None`` if
        ``session_id`` does not exist.
    """
    conversation = conv_store.get_conversation(session_id)
    # A sub-agent is neither; without this guard it would resolve to itself as a
    # (wrong) "Super Chat".
    if conversation is None or conversation.kind == "sub_agent":
        return None
    return session_id


def list_chat_roots(conv_store: ConversationStore, super_chat_id: str) -> list[Conversation]:
    """The chat a person talks in: ``[super_chat]`` (no Helpers).

    Returns ``[]`` when ``super_chat_id`` does not exist or is not a ``superside-chat``
    session. One cheap read however many Helpers the Muse has ever started.
    """
    root = conv_store.get_conversation(super_chat_id)
    if root is None or not is_superside_chat(root.labels):
        return []
    return [root]


def list_chat_family(conv_store: ConversationStore, super_chat_id: str) -> list[Conversation]:
    """The Super Chat and every Sub-agent descendant of it.

    Returns ``[]`` when ``super_chat_id`` does not exist or is not a
    ``superside-chat`` session — the Activity Feed only ever covers that
    mode (``docs/helpers.md``).

    :param conv_store: Store to query.
    :param super_chat_id: The Super Chat's conversation id.
    :returns: ``[super_chat, *sub_agents]``, each a full
        :class:`Conversation`.
    """
    chats = list_chat_roots(conv_store, super_chat_id)
    if not chats:
        return []

    sub_agents: list[Conversation] = []
    frontier = [chat.id for chat in chats]
    seen = set(frontier)
    while frontier:
        child_map = conv_store.list_child_conversation_ids_by_parent(frontier)
        next_frontier = [
            child_id
            for parent_id in frontier
            for child_id in child_map.get(parent_id, [])
            if child_id not in seen
        ]
        seen.update(next_frontier)
        if next_frontier:
            fetched = conv_store.get_conversations(next_frontier)
            sub_agents.extend(
                fetched[child_id] for child_id in next_frontier if child_id in fetched
            )
        frontier = next_frontier
    return chats + sub_agents


def is_helper_live(conversation: Conversation) -> bool:
    """Whether a Helper is not settled yet (starting, working or waiting on its parts)."""
    return (
        conversation.kind == "sub_agent" and sub_agent_status(conversation) == STATUS_IN_PROGRESS
    )
