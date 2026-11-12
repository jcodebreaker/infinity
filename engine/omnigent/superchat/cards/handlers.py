"""Runner-side handlers for the card tools: validated and returned in-process, no server hop."""

from __future__ import annotations

import json
import uuid
from typing import Any

from omnigent.superchat.cards.tools import (
    TERMINAL_CARDS,
    build_card,
    build_clarification,
    build_follow_ups,
)
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.tasks.runtime import current_round

#: What an answer card without ``message`` gets when the turn has written no words: a card alone
#: leaves the person without a sentence (after a Helper's wake the model writes no text, whatever
#: the prompt says, but it fills tool arguments), so it is sent back to carry its words.
SAY_FIRST = {
    "error": "say_first",
    "message": (
        "Not shown: the person has no words from you yet. Call render_card again with the same "
        "arguments plus `message`: one or two plain sentences saying what was made and anything "
        "still open."
    ),
}
#: What chips get when the turn has neither words nor an answer card with its ``message``.
SAY_FIRST_CHIPS = {
    "error": "say_first",
    "message": (
        "Not shown: the person has no words from you yet. Write one or two plain sentences "
        "answering them (or show the result with render_card and its `message`), then offer "
        "the chips again."
    ),
}
#: How many wordless cards or chips a turn has sent back; past :data:`_MAX_SAY_FIRST` the next
#: is taken as it is, so it never loops. Cleared at turn end.
_told: dict[str, int] = {}
_MAX_SAY_FIRST = 2
#: Conversations whose running turn showed an answer card with its ``message``: their words.
_messaged: set[str] = set()


def _say_first(ctx: HandlerCtx, refusal: dict[str, str]) -> str | None:
    """*refusal* while the turn has written no words and shown no messaged card (at most
    :data:`_MAX_SAY_FIRST` times), else ``None``."""
    conversation_id = ctx.conversation_id
    if ctx.turn_said is not False or conversation_id is None or conversation_id in _messaged:
        return None
    told = _told.get(conversation_id, 0)
    if told >= _MAX_SAY_FIRST:
        return None
    _told[conversation_id] = told + 1
    return json.dumps(refusal)


def turn_ended(conversation_id: str) -> None:
    """Forget the turn's ``say_first`` count and messaged card (``Feature.on_turn_end``)."""
    _told.pop(conversation_id, None)
    _messaged.discard(conversation_id)


async def handle_card_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Validate and echo a reply card. A terminal card carries its words in ``message``; one
    without it, in a turn that wrote no words, is sent back (:data:`SAY_FIRST`). Progress cards
    are shown mid-work and never are.

    :param ctx: The call context.
    :param args: Parsed tool arguments.
    :returns: The card as a JSON string.
    """
    card = build_card(args)
    if card.get("card") in TERMINAL_CARDS:
        if card.get("message") and ctx.conversation_id is not None:
            _messaged.add(ctx.conversation_id)
        elif not card.get("message") and (refusal := _say_first(ctx, SAY_FIRST)) is not None:
            return refusal
    return json.dumps(card, ensure_ascii=False)


async def handle_clarification_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """:returns: The ``ask`` card for an ``ask_clarification`` call, as a JSON string, naming
    the Round the Conversation's turn is in."""
    card = build_clarification(args, current_round(ctx.conversation_id), f"ask-{uuid.uuid4().hex}")
    return json.dumps(card, ensure_ascii=False)


async def handle_follow_ups_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """:returns: The ``follow_ups`` card for a ``suggest_follow_ups`` call, as a JSON string;
    :data:`SAY_FIRST_CHIPS` when the turn would otherwise end with chips and no words."""
    card = build_follow_ups(args)
    if "error" not in card and (refusal := _say_first(ctx, SAY_FIRST_CHIPS)) is not None:
        return refusal
    return json.dumps(card, ensure_ascii=False)
