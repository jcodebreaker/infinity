"""Signals (ADR 0038): what a source leaves for a person's Lookout, and the time rules. Pure.

A signal is ``{owner, kind, topic_key?, subject, due_at, stale_after, urgency, dedupe_key}``.
Code sets its urgency from its kind (never a model), its ``stale_after`` from its kind (a Library
file: the end of the next working day; a promise: the end of its date; a ``now`` kind: two
hours). When the Lookout may speak is the rhythm's (ADR 0038, merged), never decided here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NOW, NORMAL = "now", "normal"
URGENCIES = (NOW, NORMAL)

#: First sources (ADR 0038); mail (ADR 0039). Calendar and web news get kinds of their own later.
LIBRARY_FILE = "library_file"
PROMISE = "promise"
#: Mail (ADR 0039): a thread that touches the person's work.
MAIL = "mail"
#: Kinds whose rows are deleted once settled (ADR 0039, "Keep as little as possible"): no
#: excerpt, sender or subject outlives the signal.
FORGET_ON_SETTLE: frozenset[str] = frozenset({MAIL})
#: Kinds whose signals are ``now`` (meeting soon, deadline today, a blocker): none of the first
#: sources is; a later source adds its kinds here.
NOW_KINDS: frozenset[str] = frozenset()

#: Where the Judge's verdict on a signal is kept in its detail (ADR 0040, ``judge``).
JUDGE_KEY = "_judge"

#: A ``now`` signal is never told after this long.
NOW_STALE_SECONDS = 2 * 3600
#: A new ``normal`` signal waits this long before it wakes the Lookout, so a burst (one file
#: refreshed five times, three files of one page) is one wake.
SETTLE_SECONDS = 120
#: A promise's reminder is due at this local time.
PROMISE_HOUR = time(9, 0)

SUBJECT_MAX = 512
KEY_MAX = 255
TOPIC_KEY_MAX = 128


def due_words(due: str, today: date) -> str:
    """A promise's date in plain words, never an ISO date: "due today", "due tomorrow", "due
    Friday" (within the week ahead), "was due yesterday", "was due Friday" (within the week
    behind), else "due 30 Oct"."""
    try:
        day = date.fromisoformat(str(due)[:10])
    except ValueError:
        return ""
    ahead = (day - today).days
    if ahead == 0:
        return "due today"
    if ahead == 1:
        return "due tomorrow"
    if ahead == -1:
        return "was due yesterday"
    if 1 < ahead < 7:
        return f"due {day:%A}"
    if -7 < ahead < -1:
        return f"was due {day:%A}"
    return f"{'was due' if ahead < 0 else 'due'} {day.day} {day:%b}"


def zone(name: str | None) -> ZoneInfo:
    """The IANA zone *name*, UTC when empty or unknown."""
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class Signal:
    """One thing that might be worth telling the person, as a source pushes it.

    :param owner: The person (``"local"`` in single-user mode).
    :param kind: The source's kind, e.g. :data:`LIBRARY_FILE`.
    :param subject: One line naming it (a file's title, a promise's text).
    :param dedupe_key: Repeats with one key fold into one waiting signal.
    :param due_at: Epoch seconds from which it may be told.
    :param stale_after: Epoch seconds after which it is never told, only marks its topic.
    :param topic_key: Its topic when the source knows it; else the code check matches it.
    :param detail: What the code check and the run read: ``excerpt``, ``refs``, ``thread``,
        ``sender``, ``people``, ``headers``, ``item_id``, ``at`` (all optional).
    :param conversation_id: The person's Conversation when the source knows it.
    :param urgency: ``now`` or ``normal``; by default from the kind.
    """

    owner: str
    kind: str
    subject: str
    dedupe_key: str
    due_at: int
    stale_after: int
    topic_key: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None
    urgency: str = ""

    def cleaned(self) -> Signal:
        """The signal with its text cut to the columns and its urgency set from its kind."""
        urgency = self.urgency if self.urgency in URGENCIES else urgency_for(self.kind)
        return Signal(
            owner=self.owner,
            kind=self.kind[:32],
            subject=" ".join(self.subject.split())[:SUBJECT_MAX],
            dedupe_key=self.dedupe_key[:KEY_MAX],
            due_at=int(self.due_at),
            stale_after=max(int(self.stale_after), int(self.due_at)),
            topic_key=(self.topic_key or None) and self.topic_key[:TOPIC_KEY_MAX],
            detail=dict(self.detail),
            conversation_id=self.conversation_id or None,
            urgency=urgency,
        )


def urgency_for(kind: str) -> str:
    """A kind's urgency, set by code (ADR 0038, "Two urgencies")."""
    return NOW if kind in NOW_KINDS else NORMAL


def is_working_day(day: date) -> bool:
    """Monday to Friday."""
    return day.weekday() < 5


def _next_working_day(day: date) -> date:
    day += timedelta(days=1)
    while not is_working_day(day):
        day += timedelta(days=1)
    return day


def _previous_working_day(day: date) -> date:
    day -= timedelta(days=1)
    while not is_working_day(day):
        day -= timedelta(days=1)
    return day


def _end_of(day: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz).timestamp())


def stale_after_for(kind: str, at: int, tz: ZoneInfo, *, urgency: str | None = None) -> int:
    """When a signal of *kind* arriving at *at* goes stale (ADR 0038, "stale_after per kind").

    A ``now`` kind: two hours. Anything else (a Library file): the end of the next working day
    after *at*, local. A promise's is its own date (:func:`promise_times`).
    """
    if (urgency or urgency_for(kind)) == NOW:
        return at + NOW_STALE_SECONDS
    local = datetime.fromtimestamp(at, tz).date()
    return _end_of(_next_working_day(local), tz)


def promise_times(due: date, tz: ZoneInfo) -> tuple[int, int]:
    """``(due_at, stale_after)`` of a dated promise's reminder.

    Due 9:00 local on its date; when that date is not a working day (an early deadline: there is
    no working morning left before it), 9:00 the working day before. Stale at the end of its
    date: past the date a reminder is no use.
    """
    remind = due if is_working_day(due) else _previous_working_day(due)
    due_at = int(datetime.combine(remind, PROMISE_HOUR, tzinfo=tz).timestamp())
    return due_at, _end_of(due, tz)


def parse_due(value: Any) -> date | None:
    """A promise's ``due`` (``YYYY-MM-DD``) as a date; ``None`` when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
