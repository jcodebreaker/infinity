"""Full-text search across a Conversation's messages (not Helpers).

Hits are the messages a transcript shows (user and assistant text, no hidden context or system
notices), each tagged with its chat (``session_id``) and its transcript message id
(``message_id``, the item id ``GET .../transcript`` uses).
"""

from __future__ import annotations

from typing import Any

from omnigent.entities import Conversation, ConversationItem, MessageData
from omnigent.entities.conversation import is_system_notice_text
from omnigent.stores import ConversationStore
from omnigent.superchat.family.tree import list_chat_roots, resolve_super_chat_id
from omnigent.superchat.transcript.blocks import item_text


def family_chats(conv_store: ConversationStore, session_id: str) -> list[Conversation]:
    """The Conversation for *session_id*; just the session outside a family."""
    root_id = resolve_super_chat_id(conv_store, session_id)
    chats = list_chat_roots(conv_store, root_id) if root_id else []
    if chats:
        return chats
    conversation = conv_store.get_conversation(session_id)
    return [conversation] if conversation is not None else []


def shown_text(item: ConversationItem) -> str | None:
    """The text a transcript shows for *item*; ``None`` for hidden context and notices."""
    data = item.data
    if not isinstance(data, MessageData) or data.is_meta:
        return None
    flat = item.to_api_dict()
    text = item_text(flat)
    if not text:
        return None
    if data.role == "user" and (
        flat.get("is_system_notice") is True or is_system_notice_text(text)
    ):
        return None
    return text


def search_family(
    conv_store: ConversationStore,
    chats: list[Conversation],
    query: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Message hits across *chats*, newest first, at most *limit*.

    :returns: ``[{"session_id", "message_id", "role", "created_at", "text", "item"}]``:
        ``item`` is the raw item (the same shape ``GET .../items/search`` returns).
    """
    found: list[dict[str, Any]] = []
    for chat in chats:
        # Over-fetch: hidden items are dropped after ranking.
        hits = conv_store.search(query, conversation_id=chat.id, limit=limit * 3)
        for hit in hits:
            text = shown_text(hit)
            if text is None:
                continue
            assert isinstance(hit.data, MessageData)
            found.append(
                {
                    "session_id": chat.id,
                    "message_id": hit.id,
                    "role": hit.data.role,
                    "created_at": hit.created_at,
                    "text": text,
                    "item": hit.to_api_dict(),
                }
            )
    found.sort(key=lambda hit: (hit["created_at"], hit["message_id"]), reverse=True)
    return found[:limit]
