"""Reach rules (ADR 0038, "what the person says is a rule"): how the person told the assistant to
reach them, taken as is. Pure: the kinds, cleaning a rule's params, and what a set of rules
allows. The rhythm checks them before anything else (:meth:`score.Model.good`), so learned
rhythm, cold-start rules, the exploration floor and ``now`` urgency all stay inside them.

Kinds and params:

* ``pause`` ``{until?, brief?}``: no unasked message until the rule ends (or is lifted); signals
  still wait and mark their topic "New". ``brief`` is true only when the person named the
  morning brief, which then stops too.
* ``level`` ``{per_day: 0|1|3|6}``: replaces the stored level, never only lowers it: it is the
  person's newest word on how often.
* ``hours`` ``{days?, from?, to?, at?}``: replaces the hard window (quiet hours, else weekdays
  8-18). ``at`` are fixed check-in times: a message only in the :data:`CHECK_IN_S` after each.
  ``days`` default to weekdays.
* ``only_for`` ``{people?, topics?, urgency?}``: only what matches one of them gets through.
* ``mute`` ``{topic?, person?}``: never about that topic, never only about that person.

Several rules combine: any pause stops; the newest level and the newest hours win (adding one
replaces the older one anyway); what passes one ``only_for`` passes; any mute blocks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

PAUSE = "pause"
LEVEL = "level"
HOURS = "hours"
ONLY_FOR = "only_for"
MUTE = "mute"
KINDS = (PAUSE, LEVEL, HOURS, ONLY_FOR, MUTE)
#: Kinds of which a person has at most one: a new one replaces the older.
SINGLE_KINDS = frozenset({PAUSE, LEVEL, HOURS})

LEVELS = (0, 1, 3, 6)
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS = DAYS[:5]
#: How long after a fixed check-in time a message may go out.
CHECK_IN_S = 30 * 60
MAX_RULES = 20
MAX_SAID = 200
_MAX_NAMES = 10
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Rule:
    """One rule the person said.

    :param said: The person's own sentence, short.
    :param expires_at: Epoch seconds it ends, ``None`` until lifted.
    """

    id: str
    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)
    said: str = ""
    created_at: int = 0
    expires_at: int | None = None

    def active(self, at: float) -> bool:
        """Whether the rule holds at epoch seconds *at*."""
        return self.expires_at is None or at < self.expires_at


# ── cleaning ────────────────────────────────────────────────────────────────


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:80] for v in value if str(v).strip()][:_MAX_NAMES]


def _days(value: Any) -> list[str] | None:
    """``["mon", ...]`` from day names (``weekdays`` / ``weekend`` too), ``None`` when bad."""
    out: list[str] = []
    for raw in value if isinstance(value, list) else [value]:
        word = str(raw).strip().lower()
        if word in ("weekdays", "weekday"):
            out += WEEKDAYS
        elif word in ("weekend", "weekends"):
            out += DAYS[5:]
        elif word[:3] in DAYS:
            out.append(word[:3])
        else:
            return None
    return [d for d in DAYS if d in out]


def clean(kind: str, params: Any) -> dict[str, Any] | str:
    """*params* of a *kind* rule, cleaned, or a short reason it cannot be kept."""
    if kind not in KINDS:
        return f"kind must be one of {', '.join(KINDS)}"
    p = params if isinstance(params, Mapping) else {}
    if kind == PAUSE:
        out: dict[str, Any] = {"brief": p.get("brief") is True}
        if p.get("until"):
            out["until"] = str(p["until"])[:32]
        return out
    if kind == LEVEL:
        per_day = p.get("per_day")
        if isinstance(per_day, bool) or per_day not in LEVELS:
            return "level needs per_day: 0, 1, 3 or 6"
        return {"per_day": int(per_day)}
    if kind == HOURS:
        out = {}
        if p.get("days") is not None:
            days = _days(p["days"])
            if not days:
                return "hours: days are names like mon, tue, weekdays"
            out["days"] = days
        for key in ("from", "to"):
            if p.get(key) is not None:
                if not _HHMM.match(str(p[key])):
                    return f"hours: {key} is HH:MM"
                out[key] = str(p[key])
        if p.get("at") is not None:
            at = p["at"] if isinstance(p["at"], list) else [p["at"]]
            if not at or not all(_HHMM.match(str(t)) for t in at):
                return "hours: at is a list of HH:MM"
            out["at"] = sorted({str(t) for t in at})[:6]
        if not out:
            return "hours needs days, from/to or at"
        return out
    if kind == ONLY_FOR:
        out = {}
        if people := _names(p.get("people")):
            out["people"] = people
        if topics := [t.lower() for t in _names(p.get("topics"))]:
            out["topics"] = topics
        if p.get("urgency") is not None:
            if p["urgency"] != "now":
                return 'only_for: urgency can only be "now"'
            out["urgency"] = "now"
        if not out:
            return "only_for needs people, topics or urgency"
        return out
    out = {}
    if topic := str(p.get("topic") or "").strip().lower()[:80]:
        out["topic"] = topic
    if person := str(p.get("person") or "").strip()[:80]:
        out["person"] = person
    if not out:
        return "mute needs a topic or a person"
    return out


# ── what the rules allow ────────────────────────────────────────────────────


def _live(rules: Sequence[Rule], at: float, kind: str) -> list[Rule]:
    return sorted(
        (r for r in rules if r.kind == kind and r.active(at)), key=lambda r: r.created_at
    )


def paused(rules: Sequence[Rule], at: float, *, brief: bool = False) -> bool:
    """A pause holds at *at* (*brief*: one that names the morning brief)."""
    return any(not brief or r.params.get("brief") for r in _live(rules, at, PAUSE))


def per_day(rules: Sequence[Rule], at: float) -> int | None:
    """The newest level rule's messages a day, ``None`` when none holds."""
    live = _live(rules, at, LEVEL)
    return int(live[-1].params["per_day"]) if live else None


def window(rules: Sequence[Rule], local: datetime) -> bool | None:
    """Whether the newest hours rule allows local time *local*, ``None`` when none holds."""
    live = _live(rules, local.timestamp(), HOURS)
    if not live:
        return None
    p = live[-1].params
    if DAYS[local.weekday()] not in p.get("days", WEEKDAYS):
        return False
    minute = local.hour * 60 + local.minute
    if p.get("at"):
        return any(0 <= minute - _minutes(t) < CHECK_IN_S // 60 for t in p["at"])
    start, end = _minutes(p.get("from", "08:00")), _minutes(p.get("to", "18:00"))
    if start == end:
        return True
    return start <= minute < end if start < end else (minute >= start or minute < end)


def _minutes(hhmm: str) -> int:
    t = time.fromisoformat(hhmm)
    return t.hour * 60 + t.minute


def _person_in(name: str, sender: str) -> bool:
    """Whether the person *name* (a name or an address) is who *sender* names, by whole word."""
    needle, hay = name.strip().lower(), sender.lower()
    if not needle:
        return False
    if "@" in needle:
        return needle in hay
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay) is not None


def allows(
    rules: Sequence[Rule],
    at: float,
    topic_key: str | None,
    senders: Sequence[str | None] | None,
    *,
    urgent: bool,
) -> bool:
    """Whether the rules let a message through at *at* about *topic_key*, for signals from
    *senders* (one per signal, ``None`` for one without a sender; ``None`` when unknown).

    A pause stops everything. A mute stops its topic, or what comes only from its person. With
    any ``only_for`` held, it must match one: the topic, a sender, or ``now`` urgency. Signals
    on one topic are decided together (one piece of work per topic), so one from a named person
    lets the topic through.
    """
    if paused(rules, at):
        return False
    topic = (topic_key or "").lower()
    named = [s for s in senders or () if s]
    for rule in _live(rules, at, MUTE):
        if topic and rule.params.get("topic") == topic:
            return False
        person = rule.params.get("person")
        if (
            person
            and senders
            and len(named) == len(senders)
            and all(_person_in(person, s) for s in named)
        ):
            return False
    only = _live(rules, at, ONLY_FOR)
    if not only:
        return True
    for rule in only:
        p = rule.params
        if urgent and p.get("urgency") == "now":
            return True
        if topic and topic in p.get("topics", ()):
            return True
        if any(_person_in(n, s) for n in p.get("people", ()) for s in named):
            return True
    return False


# ── said back ───────────────────────────────────────────────────────────────


def line(rule: Rule, *, until: str | None = None) -> str:
    """One plain line for the rule, e.g. ``Paused (the morning brief still comes), until
    2026-10-27: "don't bother me today"``."""
    p = rule.params
    if rule.kind == PAUSE:
        head = (
            "Paused, the morning brief too"
            if p.get("brief")
            else ("Paused (the morning brief still comes)")
        )
    elif rule.kind == LEVEL:
        n = p["per_day"]
        head = "No messages" if n == 0 else f"At most {n} message{'s' if n != 1 else ''} a day"
    elif rule.kind == HOURS:
        days = ", ".join(p.get("days", WEEKDAYS))
        if p.get("at"):
            head = f"Only around {', '.join(p['at'])} on {days}"
        else:
            head = f"Only {p.get('from', '08:00')}-{p.get('to', '18:00')} on {days}"
    elif rule.kind == ONLY_FOR:
        parts = [*p.get("people", ()), *(f"topic {t}" for t in p.get("topics", ()))]
        if p.get("urgency"):
            parts.append("urgent things")
        head = "Only for " + ", ".join(parts)
    else:
        head = "Muted: " + ", ".join(
            v for v in (p.get("topic") and f"topic {p['topic']}", p.get("person")) if v
        )
    if until:
        head += f", until {until}"
    return f'{head}: "{rule.said}"' if rule.said else head


def end_of(value: str, tz: Any, *, inclusive: bool) -> int | None:
    """Epoch seconds a rule given as ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM`` (the person's zone
    *tz*) ends. A date is the last day it holds when *inclusive* (``expires_at``: "today" is
    today's date), else the day it stops holding (a pause's ``until``: messages start again
    that day). ``None`` when unreadable."""
    text = value.strip()
    try:
        if len(text) == 10:
            day = datetime.fromisoformat(text).date()
            if inclusive:
                day += timedelta(days=1)
            return int(datetime.combine(day, time.min, tzinfo=tz).timestamp())
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return int(when.timestamp())
