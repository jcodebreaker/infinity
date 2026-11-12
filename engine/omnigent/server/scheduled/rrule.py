"""RRULE (RFC 5545) next-fire computation and interval validator.

A thin wrapper over :mod:`dateutil.rrule` for the scheduled-task scheduler.
A trigger is an RFC 5545 recurrence rule string — e.g. ``"FREQ=HOURLY"`` or
``"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"`` — evaluated in a
caller-supplied IANA timezone so a preset such as "Daily at 9:00 AM" fires at
09:00 local wall-clock.

The rule is anchored at midnight of the reference day (localized to the task
timezone), so occurrence phase is deterministic: an hourly rule fires on the
hour, a daily rule at its ``BYHOUR``/``BYMINUTE``. :func:`validate_rrule`
additionally enforces a minimum interval (:data:`MIN_INTERVAL_SECONDS`) and
rejects rules that never fire or fire only once within the search window — each
fire spawns a real agent session, so a runaway cadence is expensive.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr  # type: ignore[import-untyped]

# Reject anything more frequent than this. Each fire spawns a real agent
# session, so a tight cadence gets expensive fast. One hour is the tightest
# cadence we allow: hourly is a useful ceiling with a hard bound on runaway cost.
MIN_INTERVAL_SECONDS = 60 * 60

_UTC = ZoneInfo("UTC")

# Fixed anchor for the interval check. Using a constant UTC instant (rather
# than ``datetime.now``) makes validation deterministic — the same rule always
# passes or fails regardless of when it runs. UTC has no DST, so folds can't
# perturb the sampled gaps. The anchor is a leap year (Jan 1, 2016) so a rule
# pinned to Feb 29 still reaches an occurrence within the search horizon.
_INTERVAL_ANCHOR = datetime(2016, 1, 1, tzinfo=_UTC)

# How far past the anchor to sample consecutive fires when measuring the
# minimum interval. A sub-floor gap can only occur between minute- or
# hour-adjacent fires, both of which recur within an hour, so a 25-hour span is
# guaranteed to contain any tight pair a sub-hourly cadence can produce.
_INTERVAL_WINDOW = timedelta(hours=25)

# Hard cap on how many occurrences the validator pulls from the (lazily
# generated) rule. ``dateutil`` generates on demand, so the window bound
# normally stops the walk first; this backstops the minute-cadence case, where
# 25 hours is ~1500 occurrences, against an unbounded pull.
_MAX_SAMPLE_OCCURRENCES = 2000


class RRuleValidationError(ValueError):
    """Raised when an RRULE string is malformed or violates a scheduler rule."""


class _ParsedRRule(Protocol):
    def after(self, dt: datetime, inc: bool = False) -> datetime | None:
        raise NotImplementedError

    def __iter__(self) -> Iterator[datetime]:
        raise NotImplementedError


_INTERVAL_PART = re.compile(r"(?:^|;)INTERVAL=(\d+)", re.IGNORECASE)


def _has_interval(rule_str: str) -> bool:
    """Whether the rule skips periods (``INTERVAL`` above 1), so its phase depends on dtstart."""
    match = _INTERVAL_PART.search(rule_str)
    return match is not None and int(match.group(1)) > 1


def _anchor_applies(rule_str: str, after: datetime, tz: ZoneInfo, anchor: datetime) -> bool:
    """Whether the anchor shapes this rule: it skips periods, or its start day is still ahead."""
    return _has_interval(rule_str) or anchor.astimezone(tz).date() > after.astimezone(tz).date()


def _first_occurrence(rule_str: str, start: datetime) -> datetime:
    """The first fire of the rule with ``INTERVAL`` removed, on or after ``start``.

    An interval rule counts its periods from here, not from the raw start day: "every other
    Friday starting Saturday the 17th" first fires on the first Friday after the 17th, and
    counts fortnights from that Friday (dateutil would otherwise phase weeks from the 17th's
    own week and skip that Friday).
    """
    unit_rule = _INTERVAL_PART.sub("", rule_str)
    first = _parse(unit_rule, start).after(start, inc=True)
    return first if first is not None else start


def _anchor_dtstart(after: datetime, tz: ZoneInfo, anchor: datetime | None = None) -> datetime:
    """Midnight of the local day that fixes the rule's phase.

    Without ``anchor`` this is midnight of ``after``'s local day, which gives occurrences a
    deterministic wall-clock phase whatever instant we query at: an hourly rule lands on the
    hour and a daily rule at its ``BYHOUR``/``BYMINUTE``.

    ``INTERVAL>1`` rules ("every other Friday", "every 5 hours") count periods from dtstart, so
    anchoring at the query day would slip them after every restart. The scheduler therefore
    passes the task's anchor (its start date, else the moment its rule was set): the phase is
    fixed for the life of the rule. Any rule with an anchor day still ahead waits for it.
    """
    local = (anchor or after).astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def get_next_fire_time(
    rule_str: str,
    after: datetime,
    tz: ZoneInfo,
    anchor: datetime | None = None,
) -> datetime | None:
    """Compute the next fire strictly after ``after``, evaluated in ``tz``.

    The rule is anchored at midnight (of ``after``'s local day, or of ``anchor``'s for an
    ``INTERVAL>1`` rule) so occurrences carry a deterministic wall-clock phase; the returned
    datetime is timezone-aware in ``tz``. Returns ``None`` when the rule is exhausted (a
    ``COUNT``/``UNTIL`` rule can legitimately end, unlike a bare cron).

    :param rule_str: An RFC 5545 recurrence rule, e.g. ``"FREQ=DAILY;BYHOUR=9"``.
    :param after: The instant to search after (any tz-aware datetime).
    :param tz: The IANA timezone occurrences are evaluated in.
    :param anchor: A stable instant (the task's start date, else when its rule was set) fixing
        the phase of ``INTERVAL>1`` rules and holding any rule back until its day arrives.
    :returns: The next fire as a tz-aware datetime, or ``None`` if the rule has no further
        occurrences.
    :raises RRuleValidationError: If ``rule_str`` is malformed.
    """
    use_anchor = (
        anchor if anchor is not None and _anchor_applies(rule_str, after, tz, anchor) else None
    )
    dtstart = _anchor_dtstart(after, tz, use_anchor)
    if use_anchor is not None and _has_interval(rule_str):
        dtstart = _first_occurrence(rule_str, dtstart)
    rule = _parse(rule_str, dtstart)
    # `rule.after` compares in dtstart's timezone, so localize `after` too. A
    # spring-forward "imaginary" wall time maps to some instant via zoneinfo and
    # a fall-back duplicated time picks the earlier of the two; both are
    # acceptable at an hourly floor — the schedule slips by at most an hour
    # across a DST edge.
    return rule.after(after.astimezone(tz), inc=False)


def next_fire_times(
    rule_str: str,
    after: datetime,
    tz: ZoneInfo,
    count: int = 3,
    anchor: datetime | None = None,
) -> list[datetime]:
    """The next ``count`` fires after ``after`` in ``tz`` (fewer when the rule ends)."""
    out: list[datetime] = []
    cursor = after
    while len(out) < count:
        nxt = get_next_fire_time(rule_str, cursor, tz, anchor)
        if nxt is None:
            break
        out.append(nxt)
        cursor = nxt
    return out


@dataclass(frozen=True)
class RRuleTrigger:
    """A validated RRULE string that can compute its next fire."""

    rule: str
    #: Stable instant fixing the phase of ``INTERVAL>1`` rules (start date, else rule-set time).
    anchor: datetime | None = None

    def anchored(self, anchor: datetime) -> RRuleTrigger:
        """This trigger with its phase fixed at ``anchor``."""
        return RRuleTrigger(rule=self.rule, anchor=anchor)

    def next_fire_after(self, after: datetime, tz: ZoneInfo) -> datetime | None:
        """Return the next fire strictly after ``after`` in ``tz``.

        :param after: The instant to search after (tz-aware).
        :param tz: The timezone occurrences are evaluated in.
        :returns: The next fire, or ``None`` if the rule is exhausted.
        """
        return get_next_fire_time(self.rule, after, tz, self.anchor)


def _parse(rule_str: str, dtstart: datetime) -> _ParsedRRule:
    """Parse an RRULE string anchored at ``dtstart``, normalizing errors.

    :raises RRuleValidationError: On any malformed input ``dateutil`` rejects.
    """
    try:
        return cast(_ParsedRRule, rrulestr(rule_str, dtstart=dtstart))
    except (ValueError, TypeError) as exc:
        raise RRuleValidationError(f"Invalid RRULE {rule_str!r}: {exc}") from exc


def _reject_embedded_dtstart(rule_str: str) -> None:
    """Only a single bare ``RRULE`` is accepted; the start date is a separate field.

    A ``DTSTART`` line (or any multi-line rule) would override the scheduler's anchor and carry
    a naive datetime the scheduler cannot compare, so it is refused up front.

    :raises RRuleValidationError: On a multi-line rule or one that mentions ``DTSTART``.
    """
    stripped = rule_str.strip()
    if "\n" in stripped or "\r" in stripped or "DTSTART" in stripped.upper():
        raise RRuleValidationError(
            "send a single RRULE line without DTSTART (e.g. 'FREQ=WEEKLY;BYDAY=FR'); "
            "give the first day as starts_on instead"
        )


def validate_rrule(rule_str: str, tz: ZoneInfo | None = None) -> RRuleTrigger:  # noqa: ARG001
    """Parse and validate an RRULE string for use as a recurring trigger.

    Beyond syntax, enforces that the rule (a) fires at least twice within the
    search window and (b) has a minimum gap of at least
    :data:`MIN_INTERVAL_SECONDS` between *any* two consecutive fires.

    The interval check samples fires from a fixed UTC anchor, so the verdict is
    deterministic (independent of the wall-clock instant it runs at) and immune
    to DST folds.

    :param rule_str: The RFC 5545 recurrence rule string.
    :param tz: Accepted for API compatibility but not used by the interval
        check, which is timezone-agnostic for the cadences we allow.
    :returns: An :class:`RRuleTrigger`.
    :raises RRuleValidationError: On bad syntax, never-fires, fires-once, or a
        sub-minimum interval.
    """
    _reject_embedded_dtstart(rule_str)
    rule = _parse(rule_str, _INTERVAL_ANCHOR)

    # Pull consecutive occurrences from the fixed anchor, bounded by the sample
    # window (and a hard count cap) so a lazily-generated rule can't walk
    # forever. Track the tightest gap across every consecutive pair, not just
    # the first: an irregular cadence can hide its sub-floor pair mid-window.
    prev: datetime | None = None
    window_end: datetime | None = None
    min_gap = float("inf")
    count = 0
    for occ in rule:
        count += 1
        if prev is None:
            window_end = occ + _INTERVAL_WINDOW
            prev = occ
            continue
        min_gap = min(min_gap, (occ - prev).total_seconds())
        prev = occ
        assert window_end is not None
        if occ >= window_end or count >= _MAX_SAMPLE_OCCURRENCES:
            break

    if count == 0:
        raise RRuleValidationError("RRULE never fires")
    if count == 1:
        raise RRuleValidationError("RRULE fires only once")
    if min_gap < MIN_INTERVAL_SECONDS:
        raise RRuleValidationError(
            f"Minimum interval is {MIN_INTERVAL_SECONDS // 60} minutes "
            f"(this rule fires every {int(min_gap)}s)"
        )
    return RRuleTrigger(rule=rule_str)
