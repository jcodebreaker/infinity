"""Where a session sits in a Super Chat family: kind, root, parent, open Project and folder.

Shared by the transcript route and the session snapshot (``GET /v1/sessions/{id}``), so a client
holding only a session id can tell a Super Chat from a Helper without its own map.
"""

from __future__ import annotations

from typing import Any

from omnigent.context.labels import is_superside_chat
from omnigent.entities import Conversation

# Re-exported: the open Project is plain data over the row, shared below ``superchat``.
from omnigent.entities.session_project import (
    PROJECT_NAME_LABEL_KEY as PROJECT_NAME_LABEL_KEY,
)
from omnigent.entities.session_project import open_project as open_project
from omnigent.stores import ConversationStore
from omnigent.superchat.feature import is_helper, is_super_chat
from omnigent.superchat.projects.card import workspace_folder
from omnigent.superchat.projects.choice import chosen_by_person

_MAX_HELPER_HOPS = 8


def _helper_root(conv_store: ConversationStore, conversation: Conversation) -> Conversation | None:
    """The top-level session a Helper's spawn tree hangs off (one read when the row knows it)."""
    root_id = conversation.root_conversation_id
    if root_id and root_id != conversation.id:
        return conv_store.get_conversation(root_id)
    root = conversation
    for _ in range(_MAX_HELPER_HOPS):
        if root.kind != "sub_agent" or not root.parent_conversation_id:
            break
        parent = conv_store.get_conversation(root.parent_conversation_id)
        if parent is None:
            break
        root = parent
    return root


def _super_chat_id_of(root: Conversation) -> str | None:
    """A top-level chat's Super Chat: itself (``None`` for a Helper)."""
    return None if root.kind == "sub_agent" else root.id


def session_lineage(
    conv_store: ConversationStore,
    conversation: Conversation,
    *,
    helper_root: Conversation | None = None,
) -> dict[str, Any]:
    """Where a session sits: ``kind`` (``super`` / ``helper`` / ``None``), its root and parent.

    :param helper_root: A Helper's already-read root, to skip reading it again.
    :returns: ``{"kind", "root_id", "parent_id"}``.
    """
    labels = conversation.labels
    lineage: dict[str, Any] = {"kind": None, "root_id": None, "parent_id": None}
    if conversation.kind == "sub_agent" or is_helper(labels):
        root = helper_root or _helper_root(conv_store, conversation) or conversation
        lineage.update(
            kind="helper",
            root_id=_super_chat_id_of(root),
            parent_id=conversation.parent_conversation_id,
        )
    elif is_super_chat(labels):
        lineage.update(kind="super", root_id=conversation.id)
    return lineage


def is_super_chat_helper(conv_store: ConversationStore, conversation: Conversation) -> bool:
    """True for a Helper of a Super Chat family (a sub-agent whose spawn tree is ``superside``)."""
    if conversation.kind != "sub_agent":
        return False
    root = _helper_root(conv_store, conversation)
    return root is not None and is_superside_chat(root.labels)


def session_superchat(
    conv_store: ConversationStore, conversation: Conversation
) -> dict[str, Any] | None:
    """The ``superchat`` block of a session snapshot, or ``None`` outside a Super Chat family.

    No store read for a plain session; one for a Helper (its root).
    """
    root: Conversation | None = None
    if conversation.kind == "sub_agent":
        root = _helper_root(conv_store, conversation)
        if root is None or not is_superside_chat(root.labels):
            return None
    elif not is_superside_chat(conversation.labels):
        return None
    lineage = session_lineage(conv_store, conversation, helper_root=root)
    if lineage["kind"] is None:
        return None
    return {
        **lineage,
        "project": open_project(conversation),
        "folder": workspace_folder(conversation.workspace),
        "workspace_chosen": chosen_by_person(conversation.labels),
    }
