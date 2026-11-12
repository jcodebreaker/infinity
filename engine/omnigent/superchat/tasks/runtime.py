"""Runner side of tasks: the turn's lines and its Round.

* :func:`message_prefix` reads the server's view and writes, in front of the person's message,
  "They picked ..." when the message answers a card, then one line per Round still working
  (:func:`~omnigent.superchat.tasks.record.round_line`). It keeps the turn's Round, which an
  ``ask`` card names (:func:`current_round`), until :func:`turn_ended`. No tool result is
  reported: the Round record has every tool's result from the relay.

The read is metadata, bounded per ADR 0026: a 2 s budget (:data:`BUDGET`), never blocking the
turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import httpx

from omnigent.entities.conversation import is_system_notice_text
from omnigent.superchat.tasks.record import CardAnswer, prefix_lines

_logger = logging.getLogger(__name__)

#: The budget of the read from the server (ADR 0026).
BUDGET = 2.0
_TIMEOUT = httpx.Timeout(BUDGET, connect=1.0)


#: ``Conversation id -> the Round of the turn in flight``, for every Conversation turn.
_turns: dict[str, str | None] = {}


async def read_tasks(client: httpx.AsyncClient | None, session_id: str | None) -> dict | None:
    """The server's view of *session_id* (:meth:`TaskService.view`), or ``None``."""
    if client is None or not session_id:
        return None
    try:
        resp = await asyncio.wait_for(
            client.get(f"/v1/sessions/{session_id}/tasks", timeout=_TIMEOUT), BUDGET
        )
        view = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError, TimeoutError):
        _logger.info("tasks of %s not read in time", session_id, extra={"session_id": session_id})
        return None
    return view if isinstance(view, dict) else None


def _answer(view: dict[str, Any]) -> CardAnswer | None:
    raw = view.get("answer")
    if not isinstance(raw, dict):
        return None
    try:
        return CardAnswer(str(raw["card_id"]), str(raw["option_id"]), str(raw["label"]))
    except KeyError:
        return None


async def message_prefix(
    client: httpx.AsyncClient | None, conversation_id: str, texts: Sequence[str]
) -> list[str]:
    """The pick (when the person's message answers a card) and one line per Round still working,
    as one block.

    The tasks feature is Conversation-only (``Feature.conversation_only``): a Helper's turn never
    calls this.
    """
    _turns.pop(conversation_id, None)
    view = await read_tasks(client, conversation_id)
    if view is None or view.get("conversation_id") != conversation_id:
        return []
    rounds = [r for r in view.get("rounds") or () if isinstance(r, dict)]
    by_person = view.get("by_person") is True and not any(
        is_system_notice_text(text) for text in texts
    )
    round_id = view.get("round_id") if isinstance(view.get("round_id"), str) else None
    _turns[conversation_id] = round_id
    lines = prefix_lines(rounds, _answer(view) if by_person else None)
    return ["\n".join(lines)] if lines else []


def current_round(conversation_id: str | None) -> str | None:
    """The Round the Conversation's running turn is in, or ``None``."""
    return _turns.get(conversation_id or "")


def turn_ended(conversation_id: str) -> None:
    """A turn ended (``Feature.on_turn_end``): it has no Round any more."""
    _turns.pop(conversation_id, None)
