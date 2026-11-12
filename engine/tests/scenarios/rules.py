"""When a turn ends well (ADR 0025), speaks first (ADR 0026) and keeps the machinery out of
its words (ADR 0029).

Pure functions for the offline harness and the live runner. Whether a turn ends well is the
engine's own rule (:func:`omnigent.superchat.tasks.record.ends_well`); this module only reads a
scripted turn as the engine's :class:`~omnigent.superchat.tasks.record.Turn`.

A turn is a list of events, ``(kind, detail)`` pairs. The first event opens the turn:

- ``request``: the person asks for a deliverable (a deck, page, document, sheet or file); no task
  is open yet, the turn's own work opens one;
- ``answer``: the person answers a question card, which continues an open task;
- ``message``: anything else the person says (a question, a lookup, chat);
- ``steer``: a message about work a Helper of the open task is running;
- ``notice``: a Helper's progress, flag or result waking the conversation.

What follows is what the assistant did: ``tool_call`` (detail: the tool name), ``card`` (a
question card; detail: what it asks about), ``helper_started``, ``message_helper``,
``save``, ``reply`` (detail: the text), and what the engine adds: ``cancelled`` (the person
stopped the turn), and the
work a Helper reported while it ran: ``helper_save`` (it saved a draft) and ``check`` (it ran a
check; detail: the tool).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from omnigent.superchat.tasks.record import CHECK_TOOLS, Turn
from omnigent.superchat.tasks.record import ends_well as turn_ends_well

Event = tuple[str, str]

#: Openers a person sends (the rest are the runtime's notices).
PERSON_OPENERS = frozenset({"request", "answer", "message", "steer"})
#: Openers that find a task already open.
ON_A_TASK = frozenset({"answer", "steer"})
#: Openers that make a turn a turn on a deliverable.
DELIVERABLE_OPENERS = frozenset({"request", "answer"})
#: Assistant events that do work; a deliverable turn says something before the first of them.
WORK = frozenset({"tool_call", "helper_started", "save"})
#: Assistant events the person sees as the turn's first words.
SPOKEN = frozenset({"reply"})
#: Words that hand the work back to the person instead of doing it.
GIVING_UP = (
    "send a message to continue",
    "send me a short message",
    "stop here for now",
    "stopped here for now",
)


#: Words that name the machinery instead of the work; a reply to the person says none of them.
MECHANISM_WORDS = (
    "helper",
    "helpers",
    "sub-agent",
    "sub-agents",
    "subagent",
    "subagents",
    "worker",
    "harness",
    "tool call",
    "start_helper",
    "message_helper",
    "context_brief",
    "recall_get",
    "artifact_save",
    "render_card",
    "ask_clarification",
)
_MECHANISM = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(word) for word in MECHANISM_WORDS) + r")(?![\w-])",
    re.IGNORECASE,
)
#: ADR 0029's budget, warm: seconds from the person's message to the Assistant's first words.
FIRST_WORDS_WARM = 5.0


def gives_up(text: str) -> bool:
    """Whether a reply tells the person to nudge the assistant instead of the work going on."""
    lowered = text.lower()
    return any(phrase in lowered for phrase in GIVING_UP)


def mechanism_words(text: str) -> list[str]:
    """The machinery words a reply says, lowercased, in order."""
    return [match.group(0).lower() for match in _MECHANISM.finditer(text)]


def first_words_in_time(seconds: float | None, budget: float = FIRST_WORDS_WARM) -> bool:
    """Whether the first words came within *budget*; a turn with no measured words passes."""
    return seconds is None or seconds <= budget


def ends_own_turn_after_spawn(round_events: Sequence[dict[str, Any]]) -> bool:
    """Whether the model itself ended its turn after starting a Helper, with no hook ending it."""
    del round_events
    raise NotImplementedError("needs Round events (recorded from PR 3); asserted from PR 8")


def helper_cwd_is_folder(round_events: Sequence[dict[str, Any]], folder: str) -> bool:
    """Whether each Helper of the Round ran in *folder*."""
    del round_events, folder
    raise NotImplementedError("needs the Helper's folder on its record (PR 12)")


def is_deliverable_turn(turn_events: Sequence[Event]) -> bool:
    """Whether the turn was opened by a request for a deliverable or an answer to its card."""
    return bool(turn_events) and turn_events[0][0] in DELIVERABLE_OPENERS


def as_turn(turn_events: Sequence[Event]) -> Turn:
    """A scripted turn as the engine sees it."""
    opener = turn_events[0][0] if turn_events else ""
    kinds = {kind for kind, _ in turn_events[1:]}
    return Turn(
        by_person=opener in PERSON_OPENERS,
        task_id="task" if opener in ON_A_TASK else None,
        helper_running=opener == "steer",
        saved="save" in kinds,
        asked="card" in kinds,
        helper_started="helper_started" in kinds,
    )


def ends_well(turn_events: Sequence[Event]) -> bool:
    """The engine's rule (:func:`omnigent.superchat.tasks.record.ends_well`) on a scripted turn;
    ``False`` is a stall."""
    return turn_ends_well(as_turn(turn_events))


def speaks_first(turn_events: Sequence[Event]) -> bool:
    """Whether a deliverable turn says something (a reply) before its first work call.

    A turn that is not on a deliverable, or that does no work (it ends on a question card), passes.
    """
    if not is_deliverable_turn(turn_events):
        return True
    for kind, _ in turn_events[1:]:
        if kind in SPOKEN:
            return True
        if kind in WORK:
            return False
    return True


def asks(turns: Sequence[Sequence[Event]]) -> list[str]:
    """What each question card in a conversation asked about, in order."""
    return [detail for turn in turns for kind, detail in turn if kind == "card"]


#: Events that ran the deck's check tool, in the Conversation or a Helper.
_CHECKS = frozenset({(kind, CHECK_TOOLS["deck"]) for kind in ("tool_call", "check")})


def checked(events: Sequence[Event]) -> bool:
    """Whether a deck conversation's last build or save is followed by a check that ran.

    *events* are the conversation's turns, flattened: a Helper's check arrives in the notice
    turn after the one that started it. A conversation that is not on a deliverable, that the
    person stopped, or that built nothing is not judged.
    """
    if not is_deliverable_turn(events) or any(kind == "cancelled" for kind, _ in events):
        return True
    unchecked = False
    for kind, detail in events:
        if (kind, detail) == ("tool_call", "deck_new") or kind in ("save", "helper_save"):
            unchecked = True
        elif (kind, detail) in _CHECKS:
            unchecked = False
    return not unchecked
