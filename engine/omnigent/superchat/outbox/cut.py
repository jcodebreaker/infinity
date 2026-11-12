"""What an outbox row says, cut by code from the message and the source's label (ADR 0036).

Pure: no store. The words are the message's own (ADR 0029); code only picks and trims them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from omnigent.superchat.titles import clip, plain_text

TITLE_MAX = 80
LINE_MAX = 160
URGENCIES = ("now", "today", "brief")
DEFAULT_URGENCY = "today"
#: The kind a scheduled run without one is (an automation the person set up).
DEFAULT_KIND = "automation"
#: A kind's urgency when its source names none (the daily brief's task kind, ADR 0037).
KIND_URGENCY = {"daily_brief": "brief"}
_KEY_MAX = 128
_SOURCE_MAX = 200

_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_LIST_MARK = re.compile(r"^\s*(?:[-*+>]|\d+[.)])\s+")
#: A sentence ends at ``.``, ``!`` or ``?`` followed by a space (or the end), never inside a
#: number like ``3.5``.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _first_sentence(text: str) -> tuple[str, str]:
    """``(first sentence, the rest)`` of one paragraph of plain words."""
    parts = _SENTENCE_END.split(text, maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def cut(text: str) -> tuple[str, str]:
    """``(title, line)`` of a message: the first heading, else the first sentence, as the title
    (at most :data:`TITLE_MAX` characters), and the first sentence after it as the line (at most
    :data:`LINE_MAX`). Markdown marks are dropped; either may be empty.
    """
    lines = [raw for raw in text.splitlines() if raw.strip()]
    title = ""
    if lines and (heading := _HEADING.match(lines[0])):
        title = plain_text(heading.group(1))
        lines = lines[1:]
    # The body as plain words: headings and list marks are not words, a blank line is a break.
    body = " ".join(
        plain_text(_LIST_MARK.sub("", raw))
        for raw in lines
        if not _HEADING.match(raw) and raw.strip() not in ("---", "***")
    )
    if not title:
        title, body = _first_sentence(body)
    line, _ = _first_sentence(body)
    return clip(title, TITLE_MAX), clip(line, LINE_MAX)


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:limit]


def parse_label(
    raw: str | Mapping[str, Any] | None, *, default_urgency: str = DEFAULT_URGENCY
) -> dict[str, Any]:
    """A source's proactive label (the Helper's ``omnigent.proactive`` JSON, or a notice's
    ``context.proactive`` object) as ``{kind, urgency, topic_key, source}``.

    Anything malformed is dropped, never repaired: a bad urgency is *default_urgency*, a
    missing topic is none, a kind that is not a short ``snake_case`` word is none.
    """
    if isinstance(raw, Mapping):
        value: Any = dict(raw)
    else:
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            value = {}
    if not isinstance(value, dict):
        value = {}
    kind = value.get("kind")
    urgency = value.get("urgency")
    source = {
        key: text
        for key in ("sender", "term")
        if (text := _text(value.get(key), _SOURCE_MAX)) is not None
    }
    return {
        "kind": kind if isinstance(kind, str) and _KIND.fullmatch(kind) else None,
        "urgency": urgency if urgency in URGENCIES else default_urgency,
        "topic_key": _text(value.get("topic_key"), _KEY_MAX),
        "source": source,
    }


def message_text(content: list[dict[str, Any]]) -> str:
    """The words of an assistant message's content blocks."""
    return "\n".join(
        block["text"] for block in content if isinstance(block.get("text"), str)
    ).strip()
