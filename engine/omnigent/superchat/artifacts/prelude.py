"""A file's studio prelude (ADR 0030): how Super Chat made it, read from the Conversation.

For each Round that saved a version of the file in Super Chat (not in the file's own studio),
the person's ask (the Round's opening message, the person's words only) and the Assistant's
last words in that Round (its newest reply as the transcript shows it: the text, or the card's
``message`` when the turn wrote only a card). Computed at read time from the stored items,
never copied, so it cannot drift from the Conversation. A version saved before Rounds (ADR
0029) has no ``round_id``: its story is the person's message that led to it (``item_id``) and
the replies up to the person's next message. A Round that saved several versions is told
once, at the first of them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from omnigent.entities.conversation import is_system_notice_text
from omnigent.stores import ConversationStore
from omnigent.superchat.artifacts.store import Artifact
from omnigent.superchat.artifacts.studio_turn import BRIEF_OPEN
from omnigent.superchat.transcript.blocks import item_text, project_items

_logger = logging.getLogger(__name__)

#: Items after a Round's opening message read for its replies.
_ROUND_ITEMS = 300
#: The longest ask the editor's brief line carries.
_BRIEF_MAX = 400


def from_super_chat(version: Artifact, studio_id: str) -> bool:
    """Whether Super Chat (the Assistant or a Helper of it) saved *version*, not the studio."""
    return version.origin == "ai" and version.chat_id != studio_id


def _by_person(item: dict[str, Any]) -> bool:
    if item.get("type") != "message" or item.get("role") != "user":
        return False
    if item.get("is_meta") is True or item.get("is_system_notice") is True:
        return False
    return not is_system_notice_text(item_text(item))


def _round_ids(conversation_id: str, item_ids: list[str]) -> list[str | None] | None:
    """The Round each item is in (the rounds capability), or ``None`` when unknown."""
    from omnigent.superchat.activity.derive import ROUND_IDS

    for read in ROUND_IDS.values():
        try:
            return read(conversation_id, item_ids)
        except Exception:
            _logger.warning("prelude: rounds of %s not read", conversation_id, exc_info=True)
    return None


def _story(
    conversations: ConversationStore, conversation_id: str, opener_id: str, round_id: str | None
) -> dict[str, Any] | None:
    """``{ask, handback, item_id, at}`` of the Round (or pre-Round turn) *opener_id* opened."""
    from omnigent.superchat.prompt_prefix import person_words

    found = conversations.get_item(conversation_id, opener_id)
    if found is None:
        return None
    opener = found.to_api_dict()
    after = [
        i.to_api_dict()
        for i in conversations.list_items(
            conversation_id, limit=_ROUND_ITEMS, after=opener_id, order="asc"
        ).data
    ]
    rounds = (
        _round_ids(conversation_id, [opener_id, *(str(i["id"]) for i in after)])
        if round_id is not None
        else None
    )
    if rounds is not None:
        mine = [i for i, r in zip(after, rounds[1:], strict=True) if r == round_id]
    else:  # before Rounds: the replies up to the person's next message
        mine = []
        for item in after:
            if _by_person(item):
                break
            mine.append(item)
    ask = person_words(item_text(opener)) if _by_person(opener) else ""
    messages = project_items([opener, *mine])
    reply = next(
        (
            m
            for m in reversed(messages)
            if m["role"] == "assistant" and m["blocks"] and m["blocks"][0].get("type") == "text"
        ),
        None,
    )
    handback = reply["blocks"][0]["text"] if reply is not None else ""
    if not ask and not handback:
        return None
    return {
        "ask": ask or None,
        "handback": handback or None,
        # Where *Show in Super Chat* lands: the reply, else the ask.
        "item_id": reply["id"] if reply is not None else opener_id,
        "at": opener.get("created_at"),
    }


def prelude(
    conversations: ConversationStore, versions: Sequence[Artifact], studio_id: str
) -> list[dict[str, Any]]:
    """The file's prelude, oldest first: ``[{version, round_id, ask, handback, item_id, at}]``.

    :param versions: Every version of the file (any order).
    :param studio_id: The file's studio session id; its own versions have no prelude.
    """
    first: dict[tuple[str, str], Artifact] = {}
    for version in sorted(versions, key=lambda v: v.version):
        if not from_super_chat(version, studio_id):
            continue
        key = (
            ("round", version.round_id)
            if version.round_id
            else ("item", version.item_id)
            if version.item_id
            else None
        )
        if key is not None and key not in first:
            first[key] = version
    rows: list[dict[str, Any]] = []
    for (how, opener_id), version in first.items():
        try:
            story = _story(
                conversations,
                version.parent_session_id,
                opener_id,
                opener_id if how == "round" else None,
            )
        except Exception:  # a missing story is no reason to fail opening the file
            _logger.warning("prelude: no story for %s", version.id, exc_info=True)
            story = None
        if story is not None:
            rows.append({"version": version.version, "round_id": version.round_id, **story})
    return rows


def brief_line(rows: Sequence[dict[str, Any]]) -> str | None:
    """The editor's one line on the file's brief: the newest Super Chat ask, else ``None``."""
    ask = next((r["ask"] for r in reversed(rows) if r.get("ask")), None)
    if not ask:
        return None
    words = " ".join(ask.split())
    if len(words) > _BRIEF_MAX:
        words = words[: _BRIEF_MAX - 1].rstrip() + "…"
    return f"{BRIEF_OPEN}{words}"
