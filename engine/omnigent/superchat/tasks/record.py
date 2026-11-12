"""What a turn reads in front of the person's message, and the one rule for when a turn ends
well.

Pure: no store, no HTTP. The runner writes :func:`prefix_lines` in front of the person's
message: the pick on a card, then one :func:`round_line` per Round still working (ADR 0029,
read from the Round record). The scenario evals judge each turn with :func:`ends_well`. The
task records of ADR 0025 are gone; the ``tasks`` table keeps only old rows, dropped with their
Conversation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.superchat.artifact_kinds import is_deck_name
from omnigent.superchat.titles import truncate

#: At most this many task lines go in front of a message.
MAX_LINES = 5
LINE_OPEN = "Open task: "
PICKED_LINE = "They picked {label}. Continue the task; do not plan it again."
#: Deliverable kind -> the tool that checks it; a Round's check line reads that tool's result.
CHECK_TOOLS: Mapping[str, str] = {"deck": "deck_check"}
#: Longest ask a line quotes.
_ASK_CHARS = 80
_KIND_BY_SUFFIX = {
    ".html": "page",
    ".htm": "page",
    ".md": "document",
    ".docx": "document",
    ".pdf": "document",
    ".txt": "document",
    ".csv": "sheet",
    ".xlsx": "sheet",
}


@dataclass(frozen=True)
class CardAnswer:
    """The person's pick on an ``ask`` card, sent beside the message (``context.card_answer``)."""

    card_id: str
    option_id: str
    label: str

    def to_wire(self) -> dict[str, str]:
        """``{card_id, option_id, label}``."""
        return {"card_id": self.card_id, "option_id": self.option_id, "label": self.label}


_MAX_ANSWER_CHARS = 200


def parse_card_answer(context: Mapping[str, Any] | None) -> CardAnswer | None:
    """The card answer in a message event's ``context``, or ``None`` for any other shape."""
    if not isinstance(context, Mapping):
        return None
    raw = context.get("card_answer")
    if not isinstance(raw, Mapping):
        return None
    values = [raw.get(key) for key in ("card_id", "option_id", "label")]
    if not all(isinstance(v, str) and v.strip() for v in values):
        return None
    card_id, option_id, label = (" ".join(str(v).split())[:_MAX_ANSWER_CHARS] for v in values)
    return CardAnswer(card_id, option_id, label)


def kind_for_name(name: str) -> str:
    """The task kind a saved file of this name delivers."""
    if is_deck_name(name):
        return "deck"
    low = name.lower()
    return next((k for suffix, k in _KIND_BY_SUFFIX.items() if low.endswith(suffix)), "file")


def helper_part(helper: Mapping[str, Any]) -> str:
    """A Helper of a Round not settled, as a line names it: ``helper "Uruguay deck"`` while it
    runs, else with its state (``helper "Uruguay deck" done``)."""
    named = f'helper "{helper["title"]}"' if helper.get("title") else "helper"
    status = helper.get("status")
    return named if status in (None, "running") else f"{named} {status}"


def round_line(row: Mapping[str, Any]) -> str:
    """One line naming a Round not settled (``rounds.read.open_rounds``) and, while a Helper of
    it runs, what comes next, e.g. ``Open task: "make a deck on Uruguay" · helper "Uruguay
    deck" · now: slides.html · steps done: 4 · next: wait for the helper`` (``now`` and
    ``steps`` only on the Round the message itself is in)."""
    ask = truncate(str(row.get("ask") or ""), _ASK_CHARS)
    parts = [f'{LINE_OPEN}"{ask}"' if ask else LINE_OPEN.rstrip(": ")]
    helpers = [h for h in row.get("helpers") or () if isinstance(h, Mapping)]
    parts += [helper_part(h) for h in helpers]
    if row.get("now"):
        parts.append(f"now: {row['now']}")
    if row.get("steps"):
        parts.append(f"steps done: {row['steps']}")
    if any(h.get("status") in (None, "running") for h in helpers):
        parts.append("next: wait for the helper")
    return " · ".join(parts)


def prefix_lines(rounds: Iterable[Mapping[str, Any]], answer: CardAnswer | None) -> list[str]:
    """What goes in front of the person's message: the pick first, then the Rounds still
    working (at most :data:`MAX_LINES` lines)."""
    lines = [PICKED_LINE.format(label=answer.label)] if answer is not None else []
    lines += [round_line(r) for r in rounds]
    return lines[:MAX_LINES]


@dataclass
class Turn:
    """What one Conversation turn was and did, as far as its tasks go.

    :param by_person: A person's message opened it (not a runtime notice, nor a Helper's
        progress, flag or completion wake).
    :param task_id: The open task at its start, or ``None``.
    :param helper_running: A Helper of that task was running at its start: the message steers or
        waits on work already under way.
    :param saved: A file was saved (``artifact_save``).
    :param asked: A question card was shown (``ask_clarification``).
    :param helper_started: A Helper was started (``start_helper``).
    """

    by_person: bool
    task_id: str | None = None
    helper_running: bool = False
    saved: bool = False
    asked: bool = False
    helper_started: bool = False


def ends_well(turn: Turn) -> bool:
    """Whether a turn ends as ADR 0025 says; ``False`` is a **stall**. The one definition.

    Only a turn a person opened is judged, and never one answered while a Helper of its task
    runs. It stalls when it began on an open task and ended without a save, a question card or a
    started Helper.
    """
    if not turn.by_person or turn.helper_running:
        return True
    return turn.task_id is None or turn.saved or turn.asked or turn.helper_started
