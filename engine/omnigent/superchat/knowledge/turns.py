"""Cut a chat's record into turns, and a turn into its passage for the ``conversation`` source.

Pure: items in (oldest first, the items route's shape), turns and passages out. A turn starts at a
message the person typed: a user message that is not hidden context (``is_meta``) and not a
runtime notice, the same test the transcript uses (:mod:`omnigent.superchat.transcript.blocks`).
A user message tagged with the running turn's ``response_id`` is a steer into that turn, not a
turn of its own; its words join the turn's passage. A runtime notice (a timer, a Helper's Result)
opens a turn of its own that is never indexed, so its reply is not taken for the person's.

Only the person's own words are kept: the blocks a Super Chat turn is prefixed with (the Memory
Profile, the local time, the Projects, a recall hint, attached files' text, one-shot notes) are
cut by :func:`omnigent.superchat.prompt_prefix.person_words` wherever a stored message still
carries them. A message that is nothing but those blocks opens a turn that is never indexed, like
a notice: nobody said anything, and its reply is not the answer to the turn before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from omnigent.context.tool_digest import digest_tool_items
from omnigent.entities.conversation import is_system_notice_text
from omnigent.superchat.knowledge.chunking import MAX_CHARS
from omnigent.superchat.knowledge.db import TurnPassage
from omnigent.superchat.knowledge.refs import turn_refs
from omnigent.superchat.prompt_prefix import person_words
from omnigent.superchat.transcript.blocks import item_text

#: Longest slice of the person's message and of the reply a passage keeps.
_PERSON_CHARS = 700
_ASSISTANT_CHARS = 700
#: Longest slice of the person's words kept for hint snippets.
PERSON_TEXT_CHARS = 300
#: Room a tool digest line needs to be worth adding.
_MIN_DIGEST_CHARS = 40


@dataclass
class Turn:
    """One turn of a record.

    :param turn_item_id: The id of the message that started it.
    :param first: Its position in the items it was cut from.
    :param notice: Started by a runtime notice: never indexed.
    :param at: When its first message was written (epoch seconds; 0 unknown).
    :param last_item_id: The last item of it read so far.
    """

    turn_item_id: str
    response_id: str | None
    date: str
    person: str
    first: int
    notice: bool = False
    steers: list[str] = field(default_factory=list)
    assistant: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    at: float = 0.0
    last_item_id: str = ""

    @property
    def answered(self) -> bool:
        """Whether anything came back yet (a reply or a tool call)."""
        return bool(self.assistant or self.tools)


def _date(item: dict[str, Any]) -> str:
    created = item.get("created_at")
    if isinstance(created, int | float) and not isinstance(created, bool):
        return f"{datetime.fromtimestamp(created, UTC):%Y-%m-%d}"
    return f"{datetime.now(UTC):%Y-%m-%d}"


def _at(item: dict[str, Any]) -> float:
    created = item.get("created_at")
    return (
        float(created)
        if isinstance(created, int | float) and not isinstance(created, bool)
        else 0.0
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def split_turns(items: list[dict[str, Any]]) -> list[Turn]:
    """The turns in *items* (oldest first). Items before the first turn start are skipped; every
    item after it belongs to the turn it falls in (its ``last_item_id``)."""
    turns: list[Turn] = []
    for index, item in enumerate(items):
        _take(turns, index, item)
        if turns and isinstance(item.get("id"), str):
            turns[-1].last_item_id = item["id"]
    return turns


def _take(turns: list[Turn], index: int, item: dict[str, Any]) -> None:
    """Add *item* to the last turn of *turns*, or start a turn with it."""
    kind, role = item.get("type"), item.get("role")
    current = turns[-1] if turns else None
    if kind == "message" and role == "user":
        text = item_text(item)
        if item.get("is_meta") is True or not text or not isinstance(item.get("id"), str):
            return
        rid = item.get("response_id") if isinstance(item.get("response_id"), str) else None
        text = person_words(text)
        notice = item.get("is_system_notice") is True or not text or is_system_notice_text(text)
        if current is not None and rid is not None and rid == current.response_id:
            if not notice and not current.notice:
                current.steers.append(text)
            return
        turns.append(Turn(item["id"], rid, _date(item), text, index, notice=notice, at=_at(item)))
    elif current is None:
        return
    elif kind == "message" and role == "assistant" and item.get("is_meta") is not True:
        text = item_text(item)
        if text:
            current.assistant = text  # the turn's last text is its reply
    elif kind in ("function_call", "function_call_output"):
        current.tools.append(item)


def passage_of(
    turn: Turn, *, chat_id: str, main_id: str, origin: str, project: str = ""
) -> TurnPassage:
    """The turn as one passage: the person's words, the reply, then the tool digests, clipped.

    Its references come from its tool calls; a ``project`` reference wins over *project* (the
    folder the session was in).
    """
    refs = turn_refs(turn.tools)
    project = next(iter(refs.get("project") or []), project)
    lines = [f"Person: {_clip(turn.person, _PERSON_CHARS)}"]
    lines += [f"Person: {_clip(steer, _PERSON_CHARS)}" for steer in turn.steers]
    if turn.assistant:
        lines.append(f"Assistant: {_clip(turn.assistant, _ASSISTANT_CHARS)}")
    text = "\n".join(lines)[:MAX_CHARS]
    for line in digest_tool_items(turn.tools).values():
        room = MAX_CHARS - len(text) - 1
        if room < _MIN_DIGEST_CHARS:
            break
        text += "\n" + _clip(line, room)
    return TurnPassage(
        chat_id=chat_id,
        turn_item_id=turn.turn_item_id,
        date=turn.date,
        origin=origin,
        project=project,
        person_text=_clip(" ".join(turn.person.split()), PERSON_TEXT_CHARS),
        text=text,
        main_id=main_id,
        refs={key: values for key, values in refs.items() if key != "project"},
        last_item_id=turn.last_item_id,
        at=turn.at,
    )
