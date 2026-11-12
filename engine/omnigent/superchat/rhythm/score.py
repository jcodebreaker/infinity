"""The rhythm's arithmetic (ADR 0038): situations, how behaviour scores, and the decision. Pure.

A **situation** (cell) is a day-part (morning 6-11, midday 11-14, afternoon 14-18, evening
18-23) x weekday or weekend x what the person is doing (``chatting`` while a turn runs, else
``not``): 16 cells. Night (23-6) is no cell.

A proactive message scores once, for the cell it was sent in, from the strongest thing that
happened to it:

* acted on (Open topic, its file or draft, or its topic touched in the Conversation within
  :data:`TOUCH_WINDOW_S`): strong yes, ``+1`` yes;
* opened: weak yes, ``+0.5`` yes;
* not shown within 24 hours (the sweep's ``unseen``): weak no, ``+0.5`` no;
* shown without more: neutral, nothing.

Each cell's yes / no decay with a half-life of :data:`HALF_LIFE_S`, so an old verdict fades and a
closed cell is tried again. A cell's rate is smoothed toward the person's own overall rate
(Beta-Binomial: ``(yes + K*p) / (yes + no + K)``, ``p`` the overall rate under a Beta(1, 1)
prior). A cell with evidence below :data:`MIN_EVIDENCE` keeps today's rule.

**What the person said comes first** (:mod:`.reach`): their reach rules are checked before
anything here; an ``hours`` rule replaces the hard window and a ``level`` rule the level.

**The decision only ever makes things quieter.** The hard window is the person's own hours
(outside their quiet hours) when they set some, else weekdays 8:00-18:00; the level caps the day
(off / low / normal / high = 0 / 1 / 3 / 6, the brief not counted); one message per topic per 3
hours. Inside all that, once :data:`LEARN_DAYS` days have events, a cell with enough evidence and
a smoothed rate below :data:`THRESHOLD` says no.

**Exploration floor.** Closed cells get no new evidence, so a person who reads rarely (or
elsewhere) would stay silenced until decay. When no counted message went out in the last
:data:`EXPLORE_WORKDAYS` working days (weekdays), the in-window cell with the best smoothed rate
(any in-window moment when none has evidence) is allowed once, still inside every ceiling: one
exploratory message per three working days at most.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.superchat.rhythm import reach

CHATTING = "chatting"
NOT = "not"
STATES = (CHATTING, NOT)

#: ``(name, first hour, end hour)`` in the person's local time.
DAY_PARTS = (("morning", 6, 11), ("midday", 11, 14), ("afternoon", 14, 18), ("evening", 18, 23))
DAY_TYPES = ("weekday", "weekend")
CELLS = tuple(f"{p}:{d}:{s}" for p, _a, _b in DAY_PARTS for d in DAY_TYPES for s in STATES)

SENT = "sent"
SHOWN = "shown"
OPENED = "opened"
ACTED_ON = "acted_on"
UNSEEN = "unseen"
#: What a client may report; ``sent`` is the engine's (with the state), ``unseen`` the sweep's.
CLIENT_EVENTS = (SHOWN, OPENED, ACTED_ON)
EVENTS = (SENT, SHOWN, OPENED, ACTED_ON, UNSEEN)

#: A message not shown this long after it was sent is a weak no.
UNSEEN_AFTER_S = 24 * 3600
#: The topic touched in the Conversation this long after the message counts as acting on it.
TOUCH_WINDOW_S = 3 * 3600
HALF_LIFE_S = 14 * 24 * 3600
#: Pseudo-count pulling a cell toward the person's overall rate.
PRIOR_K = 2.0
MIN_EVIDENCE = 1.0
THRESHOLD = 0.35
#: Distinct local days with events before the learned cells count.
LEARN_DAYS = 4

#: Messages per local day by proactivity level (topic watch's rule, ADR 0035).
DAILY_CAP = {"off": 0, "low": 1, "normal": 3, "high": 6}
TOPIC_WINDOW_S = 3 * 3600
#: Kinds that are promises, never counted against the day's cap (ADR 0037's brief).
UNCOUNTED_KINDS = frozenset({"daily_brief"})
WORK_START, WORK_END = 8, 18
#: Working days without a counted message before one exploratory message is allowed.
EXPLORE_WORKDAYS = 3


def zone(name: str | None) -> ZoneInfo:
    """The IANA zone *name*, UTC when empty or unknown."""
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def cell_of(local: datetime, state: str) -> str | None:
    """The situation of local time *local* in *state*, ``None`` at night."""
    day = "weekend" if local.weekday() >= 5 else "weekday"
    for name, start, end in DAY_PARTS:
        if start <= local.hour < end:
            return f"{name}:{day}:{CHATTING if state == CHATTING else NOT}"
    return None


def outcome(events: Iterable[str]) -> tuple[float, float]:
    """``(yes, no)`` one message adds to its cell, from the events it has."""
    have = set(events)
    if ACTED_ON in have:
        return 1.0, 0.0
    if OPENED in have:
        return 0.5, 0.0
    if UNSEEN in have:
        return 0.0, 0.5
    return 0.0, 0.0


def decayed(value: float, age_s: float) -> float:
    """*value* after *age_s* seconds of half-life decay."""
    return value * 0.5 ** (max(age_s, 0.0) / HALF_LIFE_S)


@dataclass(frozen=True)
class Tally:
    """One cell's counts as of ``at`` (already decayed)."""

    yes: float = 0.0
    no: float = 0.0


def _topics(value: str | frozenset[str] | None) -> frozenset[str]:
    """A sent message's topics, from one key or a set of them."""
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(value or ())


@dataclass(frozen=True)
class Prefs:
    """What the person set: level, quiet hours (``HH:MM`` or ``None``) and time zone."""

    level: str = "normal"
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str = "UTC"


@dataclass
class Model:
    """Everything one decision needs, read once: the person's tallies, prefs and recent sends.

    :param tallies: ``cell -> Tally`` decayed to now.
    :param learned: Whether :data:`LEARN_DAYS` days of events exist.
    :param sent: ``(created_at epoch s, kind, topics)`` of recent proactive messages, *topics*
        every topic key it covers (a set) or one key (``str``/``None``).
    :param rules: The person's reach rules (:mod:`.reach`), hard ceilings over everything here.
    """

    prefs: Prefs
    tallies: Mapping[str, Tally] = field(default_factory=dict)
    learned: bool = False
    sent: list[tuple[float, str, str | frozenset[str] | None]] = field(default_factory=list)
    rules: Sequence[reach.Rule] = field(default_factory=list)
    _best: dict[str, float] = field(default_factory=dict, repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return zone(self.prefs.timezone)

    def overall(self) -> float:
        """The person's overall yes rate, Beta(1, 1) prior."""
        yes = sum(t.yes for t in self.tallies.values())
        no = sum(t.no for t in self.tallies.values())
        return (yes + 1.0) / (yes + no + 2.0)

    def rate(self, cell: str) -> float | None:
        """The cell's smoothed rate, ``None`` while it has too little evidence."""
        t = self.tallies.get(cell, Tally())
        if t.yes + t.no < MIN_EVIDENCE:
            return None
        return (t.yes + PRIOR_K * self.overall()) / (t.yes + t.no + PRIOR_K)

    # ── the hard ceilings ───────────────────────────────────────────────────

    def in_window(self, local: datetime) -> bool:
        """The hard window: the person's hours rule when they said one, else outside their quiet
        hours when they set them, else weekdays 8:00-18:00 (ADR 0038, quiet hours default)."""
        ruled = reach.window(self.rules, local)
        if ruled is not None:
            return ruled
        start, end = self.prefs.quiet_start, self.prefs.quiet_end
        if start and end and start != end:
            s, e, t = time.fromisoformat(start), time.fromisoformat(end), local.time()
            inside = (s <= t < e) if s < e else (t >= s or t < e)
            return not inside
        return local.weekday() < 5 and WORK_START <= local.hour < WORK_END

    def day_full(self, local: datetime) -> bool:
        """The level's messages for *local*'s day are used up (``off`` always is). A level the
        person said replaces the stored one."""
        cap = reach.per_day(self.rules, local.timestamp())
        if cap is None:
            cap = DAILY_CAP.get(self.prefs.level, DAILY_CAP["normal"])
        if cap <= 0:
            return True
        day = local.date()
        used = sum(
            1
            for at, kind, _topic in self.sent
            if kind not in UNCOUNTED_KINDS and datetime.fromtimestamp(at, self.tz).date() == day
        )
        return used >= cap

    def topic_busy(self, at: float, topic_key: str | None) -> bool:
        """A message covering *topic_key* (any of its topics) went out less than 3 hours
        before *at*."""
        if not topic_key:
            return False
        return any(
            topic_key in _topics(topics) and 0 <= at - sent < TOPIC_WINDOW_S
            for sent, _kind, topics in self.sent
        )

    # ── the decision ────────────────────────────────────────────────────────

    def good(
        self,
        at: datetime,
        state: str,
        topic_key: str | None = None,
        *,
        urgent: bool = False,
        senders: Sequence[str | None] | None = None,
    ) -> bool:
        """Whether *at* in *state* is a good moment: the person's rules, the ceilings, then what
        was learned. *urgent* (a ``now`` signal, ADR 0038 "Two urgencies") skips what was
        learned, never the rules. *senders*: who the signals in question come from."""
        if not reach.allows(self.rules, at.timestamp(), topic_key, senders, urgent=urgent):
            return False
        local = at.astimezone(self.tz)
        if not self.in_window(local) or self.day_full(local):
            return False
        if self.topic_busy(at.timestamp(), topic_key):
            return False
        if urgent or not self.learned:
            return True
        cell = cell_of(local, state)
        rate = self.rate(cell) if cell else None
        if rate is None or rate >= THRESHOLD:
            return True
        return self._exploring(local) and rate >= self._best_rate(state)

    def _exploring(self, local: datetime) -> bool:
        """No counted message went out since the start of the working day
        :data:`EXPLORE_WORKDAYS` working days before *local*'s."""
        day, back = local.date(), 0
        while back < EXPLORE_WORKDAYS:
            day -= timedelta(days=1)
            if day.weekday() < 5:
                back += 1
        since = datetime.combine(day, time.min, tzinfo=self.tz).timestamp()
        return not any(
            at >= since for at, kind, _topic in self.sent if kind not in UNCOUNTED_KINDS
        )

    def _best_rate(self, state: str) -> float:
        """The best smoothed rate among cells the hard window reaches (hourly over a week)."""
        if state in self._best:
            return self._best[state]
        start = datetime(2026, 1, 5, tzinfo=self.tz)  # a Monday
        best = 0.0
        for hour in range(7 * 24):
            local = start + timedelta(hours=hour)
            if self.in_window(local) and (cell := cell_of(local, state)):
                rate = self.rate(cell)
                best = max(best, rate if rate is not None else 1.0)
        self._best[state] = best
        return best

    def next_slot(
        self,
        after: datetime,
        state: str,
        topic_key: str | None = None,
        *,
        days: int = 8,
        urgent: bool = False,
        senders: Sequence[str | None] | None = None,
    ) -> datetime | None:
        """The first good moment at or after *after* (15-minute steps), ``None`` within *days*."""
        if self.good(after, state, topic_key, urgent=urgent, senders=senders):
            return after
        step = timedelta(minutes=15)
        local = after.astimezone(self.tz)
        candidate = local.replace(minute=(local.minute // 15) * 15, second=0, microsecond=0) + step
        end = after + timedelta(days=days)
        while candidate <= end:
            if self.good(candidate, state, topic_key, urgent=urgent, senders=senders):
                return candidate
            candidate += step
        return None


#: How a day-part reads after "on weekday" / "on weekend".
_PART_WORDS = {
    "morning": "mornings",
    "midday": "middays",
    "afternoon": "afternoons",
    "evening": "evenings",
}


def _clock(value: str) -> str:
    """``"07:00"`` as ``"7:00"``."""
    return value[1:] if value.startswith("0") and len(value) == 5 else value


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def summary(model: Model) -> str:
    """One plain line saying what was learned (ADR 0038), e.g. "I usually reach out on weekday
    mornings and afternoons, rarely while you're chatting."."""
    prefs = model.prefs
    if DAILY_CAP.get(prefs.level, DAILY_CAP["normal"]) <= 0:
        return "You've turned my messages off, so I only speak when you write to me."
    if prefs.quiet_start and prefs.quiet_end and prefs.quiet_start != prefs.quiet_end:
        window = (
            f"outside your quiet hours ({_clock(prefs.quiet_start)}–{_clock(prefs.quiet_end)})"
        )
    else:
        window = "on weekdays between 8:00 and 18:00"
    if not model.learned:
        return f"I'm still learning when you like to hear from me; for now I reach out {window}."
    phrases, held = [], False
    for day in DAY_TYPES:
        good = []
        for name, _start, _end in DAY_PARTS:
            rate = model.rate(f"{name}:{day}:{NOT}")
            if rate is None:
                continue
            if rate >= THRESHOLD:
                good.append(_PART_WORDS[name])
            else:
                held = True
        if good:
            phrases.append(f"on {day} {_and(good)}")
    if phrases:
        line = "I usually reach out " + " and ".join(phrases)
    elif held:
        line = "My messages have mostly gone unread lately, so I'm holding back"
    else:
        line = f"I reach out {window}"
    chatting = [
        rate
        for name, _start, _end in DAY_PARTS
        for day in DAY_TYPES
        if (rate := model.rate(f"{name}:{day}:{CHATTING}")) is not None
    ]
    if chatting:
        line += (
            ", rarely while you're chatting"
            if max(chatting) < THRESHOLD
            else ", also while you're chatting"
        )
    return line + "."
