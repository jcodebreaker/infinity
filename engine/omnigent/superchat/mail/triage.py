"""Which new mail becomes a Lookout signal (ADR 0039, amended by 0040: code only removes what
can never matter). Pure.

Per thread with new mail, the newest new message not from the person is read:

1. **Noise** (list, bulk, automated, no-reply or notification senders, auto-replies): dropped.
   Decided by the Lookout's own code check (``check``, handed in by the composition root), never
   copied here.
2. **Not addressed** to the person (``me`` neither in ``To`` nor ``Cc``): dropped.
3. **Everything else is a signal** for the Lookout's Judge, with the check's match as a hint:
   the topic and how strongly it matched (a watched sender, watched terms, a watched file), or
   "no topic match". A thread the person wrote in is marked ``direct``. The Judge decides how
   much it matters, not this code.

One signal per thread (``mail:<thread>``); a newer mail updates it. When the person's own reply
is the thread's newest message, the waiting signal is removed (``answered``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.superchat.mail.types import Message

KIND = "mail"
#: The longest excerpt a mail signal keeps (ADR 0039).
EXCERPT_CHARS = 800
#: A message older than this when first seen is only marked seen (a first poll never replays
#: an old inbox).
MAX_AGE_SECONDS = 14 * 86400
_QUOTED = re.compile(r"^\s*>.*$", re.MULTILINE)
_FILE_NAME = re.compile(
    r"(?<![\w/])[\w][\w.-]*\.(?:pptx?|xlsx?|docx?|pdf|html?|csv|md|key|numbers|pages)\b",
    re.IGNORECASE,
)

#: ``fields -> verdict``: the Lookout's code check for one item (``{"noise": why}``, or
#: ``{"topic_key", "strength", "reason"}``, or ``{}`` for no topic).
Check = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class Decision:
    """What the poll does about one thread.

    :param thread: The thread.
    :param action: ``signal``, ``drop`` or ``answered`` (remove the waiting signal).
    :param reason: Plain words, for the log.
    :param signal: The signal's fields for ``signal``.
    """

    thread: str
    action: str
    reason: str
    signal: dict[str, Any] | None = None


def dedupe_key(thread: str) -> str:
    """The one signal a thread has."""
    return f"{KIND}:{thread}"


def excerpt(body: str, limit: int = EXCERPT_CHARS) -> str:
    """A body without quoted lines, whitespace collapsed, cut to *limit* characters."""
    flat = " ".join(_QUOTED.sub(" ", body).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def file_names(text: str) -> list[str]:
    """File names a mail mentions (``Q3 numbers.xlsx`` is read as ``numbers.xlsx``)."""
    return sorted({m.group(0) for m in _FILE_NAME.finditer(text)})


def _fields(message: Message, *, text: str | None = None) -> dict[str, Any]:
    """*message* as the Lookout's check reads an item (*text* replaces its body)."""
    body = message.body if text is None else text
    return {
        "source": KIND,
        "id": message.id,
        "subject": message.subject,
        "excerpt": excerpt(body, 4000),
        "sender": message.sender,
        "people": [p for p in (message.to, message.cc) if p],
        "refs": file_names(f"{message.subject}\n{body}"),
        "headers": dict(message.headers),
        "thread": message.thread,
        "at": message.at,
    }


def triage(
    messages: Iterable[Message],
    seen: set[str],
    *,
    owner: str,
    me: str | None,
    check: Check,
    now: int,
) -> list[Decision]:
    """What to do about each thread with a message not in *seen* (oldest thread first)."""
    threads: dict[str, list[Message]] = {}
    for message in sorted(messages, key=lambda m: m.at):
        threads.setdefault(message.thread, []).append(message)
    decisions: list[Decision] = []
    for thread, msgs in threads.items():
        new = [m for m in msgs if m.id not in seen and now - m.at <= MAX_AGE_SECONDS]
        if not new:
            continue
        mine = [m for m in msgs if me and m.from_address() == me]
        others = [m for m in new if not (me and m.from_address() == me)]
        if mine and mine[-1] is msgs[-1] and any(m in new for m in mine):
            decisions.append(Decision(thread, "answered", "the person replied last"))
            continue
        if not others:
            continue
        latest = others[-1]
        verdict = dict(check(_fields(latest)) or {})
        if verdict.get("noise"):
            decisions.append(Decision(thread, "drop", f"noise: {verdict['noise']}"))
            continue
        if me and me not in latest.recipients():
            decisions.append(Decision(thread, "drop", "not addressed to the person"))
            continue
        wrote_in = bool(mine)
        if not verdict.get("topic_key") and wrote_in:
            # The person's own words in the thread often name the topic the new mail does not.
            whole = "\n\n".join(f"{m.subject}\n{m.body}" for m in msgs)
            verdict = dict(check(_fields(latest, text=whole)) or {})
            verdict.pop("noise", None)
        topic = verdict.get("topic_key") or None
        reason = verdict.get("reason") or (
            "a thread the person wrote in" if wrote_in else "no topic match"
        )
        hint = (
            {"match_strength": verdict.get("strength"), "match_reason": reason}
            if topic and verdict.get("strength")
            else {}
        )
        decisions.append(
            Decision(
                thread,
                "signal",
                f"{topic or 'no topic'}: {reason}",
                signal=_signal(owner, latest, topic, wrote_in, len(others), now, hint),
            )
        )
    return decisions


def _signal(
    owner: str,
    latest: Message,
    topic: str | None,
    wrote_in: bool,
    count: int,
    now: int,
    hint: Mapping[str, Any],
) -> dict[str, Any]:
    """The signal's fields (``stale_after`` 0: the kind's default, end of next working day)."""
    return {
        "owner": owner,
        "kind": KIND,
        "subject": latest.subject,
        "dedupe_key": dedupe_key(latest.thread),
        "due_at": now,
        "stale_after": 0,
        "topic_key": topic,
        "detail": {
            "source": KIND,
            "item_id": latest.id,
            "thread": latest.thread,
            "sender": latest.sender,
            "subject": latest.subject,
            "date": latest.date.isoformat(),
            "at": latest.at,
            "excerpt": excerpt(latest.body),
            "refs": file_names(f"{latest.subject}\n{latest.body}"),
            "headers": dict(latest.headers),
            "direct": wrote_in,
            "new_messages": count,
            # The code's match, a hint for the Lookout's Judge; without a topic the Lookout
            # matches the signal itself (its topics may have moved since the poll).
            **hint,
        },
    }
