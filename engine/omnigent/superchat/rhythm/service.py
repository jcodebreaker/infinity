"""The rhythm service (ADR 0038): reads the person's tallies, preferences and recent proactive
messages once, then decides in code. No model is ever called here.

Which messages were proactive (and when, with which topic) comes from the outbox through core
(:func:`omnigent.superchat.feature.proactive_items`); the level, quiet hours and time zone from
the owner's preferences. Before each read, a small sweep marks messages that nobody was shown
within 24 hours as ``unseen`` (a weak no). The person's reach rules are read with everything
else and decide first (:mod:`omnigent.superchat.rhythm.reach`).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from omnigent.superchat.feature import proactive_item, proactive_items
from omnigent.superchat.rhythm import reach
from omnigent.superchat.rhythm.score import (
    ACTED_ON,
    CHATTING,
    EVENTS,
    LEARN_DAYS,
    NOT,
    SENT,
    TOUCH_WINDOW_S,
    UNSEEN,
    UNSEEN_AFTER_S,
    Model,
    Prefs,
    cell_of,
    summary,
    zone,
)
from omnigent.superchat.rhythm.store import SqlAlchemyRhythmStore

_logger = logging.getLogger(__name__)

#: How far back the sweep looks for messages nobody was shown.
_SWEEP_BACK_S = 7 * 24 * 3600
#: How far back recent sends are read for the day's cap, the topic window and the exploration
#: floor (three working days, across a weekend and a holiday).
_SENT_BACK_S = 7 * 24 * 3600
#: ``fn(owner)`` told after the person added or lifted a reach rule, so whoever holds messages
#: for a closed window (the Lookout) looks again now; registered through
#: :func:`omnigent.superchat.rhythm.register_rules_changed`. A rule that ends needs no call:
#: ``next_good_slot`` already returns its end.
RULES_CHANGED: list[Callable[[str], None]] = []

#: How far ahead ``next_good_slot`` looks before giving up.
_HORIZON_DAYS = 8


def _epoch(at: datetime) -> int:
    return int((at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp())


class Rhythm:
    """``good_moment``, ``next_good_slot`` and ``record`` over one store."""

    def __init__(self, store: SqlAlchemyRhythmStore, scheduled_task_store: Any) -> None:
        self._store = store
        self._prefs_store = scheduled_task_store

    # ── reading ─────────────────────────────────────────────────────────────

    def _prefs(self, owner: str) -> Prefs:
        prefs = None
        if self._prefs_store is not None:
            try:
                prefs = self._prefs_store.get_owner_preferences(owner)
            except Exception:  # an unreadable preference never opens the gate wider
                _logger.warning("rhythm: preferences unreadable", exc_info=True)
        if prefs is None:
            return Prefs()
        return Prefs(
            level=prefs.proactivity or "normal",
            quiet_start=prefs.quiet_start,
            quiet_end=prefs.quiet_end,
            timezone=prefs.timezone or "UTC",
        )

    def model(self, owner: str, *, now: int | None = None) -> Model:
        """Everything a decision needs about *owner*, read once (after the sweep)."""
        at = now if now is not None else int(time.time())
        self.sweep(owner, now=at)
        prefs = self._prefs(owner)
        tz = zone(prefs.timezone)
        days = {
            datetime.fromtimestamp(t, tz).date() for t in self._store.event_times(owner, now=at)
        }
        rows = proactive_items(owner, (at - _SENT_BACK_S) * 1000, (at + 1) * 1000)
        # A message covering several topics counts against each (its ``sent`` names them all).
        covered = self._store.sent_topics(owner, [str(r["item_id"]) for r in rows])
        sent = [
            (
                row["created_at_ms"] / 1000,
                str(row.get("kind") or ""),
                frozenset(
                    k for k in (row.get("topic_key"), *covered.get(str(row["item_id"]), ())) if k
                ),
            )
            for row in rows
        ]
        return Model(
            prefs=prefs,
            tallies=self._store.tallies(owner, now=at),
            learned=len(days) >= LEARN_DAYS,
            sent=sent,
            rules=self._store.rules(owner, now=at),
        )

    def good_moment(
        self,
        owner: str,
        at: datetime,
        state: str,
        *,
        topic_key: str | None = None,
        urgent: bool = False,
        senders: Sequence[str | None] | None = None,
    ) -> bool:
        """Whether *at* is a good moment to message *owner* unasked while they are *state*
        (``chatting`` or ``not``); *topic_key* adds the one-per-topic-per-3-hours ceiling;
        *urgent* checks the ceilings only, never what was learned; the person's rules always
        (*senders*: who the signals come from, for ``only_for`` and ``mute``)."""
        return self.model(owner).good(at, _state(state), topic_key, urgent=urgent, senders=senders)

    def next_good_slot(
        self,
        owner: str,
        after: datetime,
        state: str,
        *,
        topic_key: str | None = None,
        urgent: bool = False,
        senders: Sequence[str | None] | None = None,
    ) -> datetime:
        """The first good moment at or after *after*; when none falls within eight days (the
        level is off, a pause until lifted, or everything was learned closed), the end of that
        horizon, when the Lookout looks again."""
        found = self.model(owner).next_slot(
            after, _state(state), topic_key, days=_HORIZON_DAYS, urgent=urgent, senders=senders
        )
        return found if found is not None else after + timedelta(days=_HORIZON_DAYS)

    def summary(self, owner: str) -> dict[str, Any]:
        """``{line, learning}``: the plain line Settings shows."""
        model = self.model(owner)
        return {"line": summary(model), "learning": not model.learned}

    @staticmethod
    def is_proactive(owner: str, item_id: str) -> bool:
        """Whether *item_id* is one of the owner's proactive messages."""
        return proactive_item(owner, item_id) is not None

    # ── writing ─────────────────────────────────────────────────────────────

    def record(
        self,
        owner: str,
        item_id: str,
        event: str,
        at: datetime,
        *,
        state: str | None = None,
        topic_keys: list[str] | None = None,
    ) -> bool:
        """Keep one behaviour *event* on the owner's proactive message *item_id*.

        ``sent`` is the engine's (the poster passes the *state* the person was in, which places
        the message in a cell); the rest need the message to be one of the owner's proactive
        messages, whose cell is its send time with the state its ``sent`` carried, else ``not``
        (today a proactive turn never starts while the person's own turn runs). A ``sent`` may
        name every topic the message covers (*topic_keys*); each then counts for the
        one-per-topic limit.

        :returns: ``False`` when it is not one of the owner's proactive messages, the event is
            unknown, or it was already recorded.
        """
        if event not in EVENTS:
            return False
        when = _epoch(at)
        prefs = self._prefs(owner)
        tz = zone(prefs.timezone)
        if event == SENT:
            cell = cell_of(datetime.fromtimestamp(when, tz), _state(state or NOT))
            return self._store.add_event(
                owner, item_id, SENT, when, cell, topic_keys=list(topic_keys or [])
            )
        row = proactive_item(owner, item_id)
        if row is None:
            return False
        sent_at = row["created_at_ms"] / 1000
        if sent_at < self._store.started_at(owner):
            return False  # from before Start over
        cell = cell_of(datetime.fromtimestamp(sent_at, tz), NOT)
        return self._store.add_event(owner, item_id, event, when, cell)

    def topic_touched(self, owner: str, topic_key: str, at: int, item_id: str | None) -> None:
        """The person took up *topic_key*: the message the pick came from, and every proactive
        message on that topic of the last few hours, were acted on (a strong yes)."""
        moment = datetime.fromtimestamp(at, UTC)
        if item_id:
            self.record(owner, item_id, ACTED_ON, moment)
        for row in proactive_items(owner, (at - TOUCH_WINDOW_S) * 1000, (at + 1) * 1000):
            if row.get("topic_key") == topic_key and row["item_id"] != item_id:
                self.record(owner, str(row["item_id"]), ACTED_ON, moment)

    def sweep(self, owner: str, *, now: int | None = None) -> int:
        """Mark the owner's messages nobody was shown within 24 hours ``unseen`` (no model).

        :returns: How many were marked.
        """
        at = now if now is not None else int(time.time())
        since = max(at - _SWEEP_BACK_S, self._store.started_at(owner))
        rows = proactive_items(owner, since * 1000, (at - UNSEEN_AFTER_S) * 1000)
        if not rows:
            return 0
        ids = [str(r["item_id"]) for r in rows]
        seen = self._store.with_events(owner, ids)
        tz = zone(self._prefs(owner).timezone)
        marked = 0
        for row in rows:
            item = str(row["item_id"])
            if item in seen:
                continue
            sent_at = row["created_at_ms"] / 1000
            cell = cell_of(datetime.fromtimestamp(sent_at, tz), NOT)
            if self._store.add_event(owner, item, UNSEEN, int(sent_at) + UNSEEN_AFTER_S, cell):
                marked += 1
        return marked

    def reset(self, owner: str) -> None:
        """*Start over*: what was learned only, never the person's rules."""
        self._store.reset(owner)

    # ── reach rules ─────────────────────────────────────────────────────────

    def rules(self, owner: str) -> list[dict[str, Any]]:
        """The owner's rules in force, oldest first, each with its plain ``line``."""
        tz = zone(self._prefs(owner).timezone)
        return [_shown(r, tz) for r in self._store.rules(owner)]

    def add_rule(
        self,
        owner: str,
        kind: str,
        params: Any,
        said: str,
        expires_at: str | None = None,
    ) -> dict[str, Any] | str:
        """Keep one rule the person said; a newer ``pause``, ``level`` or ``hours`` replaces
        the older one. *expires_at* (and a pause's ``until``) are dates in the person's zone.

        :returns: ``{rule, replaced}``, or a short reason it was not kept.
        """
        cleaned = reach.clean(kind, params)
        if isinstance(cleaned, str):
            return cleaned
        tz = zone(self._prefs(owner).timezone)
        now = int(time.time())
        ends: list[int] = []
        for value, inclusive in ((expires_at, True), (cleaned.get("until"), False)):
            if not value:
                continue
            end = reach.end_of(str(value), tz, inclusive=inclusive)
            if end is None:
                return f"could not read the date {value!r}; use YYYY-MM-DD"
            ends.append(end)
        end = min(ends) if ends else None
        if end is not None and end <= now:
            return "that date has already passed"
        held = self._store.rules(owner, now=now)
        replaced = [r for r in held if kind in reach.SINGLE_KINDS and r.kind == kind]
        if len(held) - len(replaced) >= reach.MAX_RULES:
            return f"at most {reach.MAX_RULES} rules; remove one first"
        rule = reach.Rule(
            id=uuid.uuid4().hex,
            kind=kind,
            params=cleaned,
            said=" ".join(str(said or "").split())[: reach.MAX_SAID],
            created_at=now,
            expires_at=end,
        )
        self._store.add_rule(owner, rule, replace=[r.id for r in replaced])
        _rules_changed(owner)
        return {"rule": _shown(rule, tz), "replaced": [_shown(r, tz) for r in replaced]}

    def remove_rules(
        self, owner: str, *, rule_id: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Lift the rule *rule_id*, or every rule of *kind* ("you can message me again" lifts
        the pause); what was lifted."""
        tz = zone(self._prefs(owner).timezone)
        gone = [
            r
            for r in self._store.rules(owner)
            if (rule_id and r.id == rule_id) or (kind and not rule_id and r.kind == kind)
        ]
        if gone:
            self._store.remove_rules(owner, [r.id for r in gone])
            _rules_changed(owner)
        return [_shown(r, tz) for r in gone]

    def brief_paused(self, owner: str) -> bool:
        """Whether the person paused the morning brief too (they named it)."""
        return reach.paused(self._store.rules(owner), time.time(), brief=True)


def _rules_changed(owner: str) -> None:
    """Tell every :data:`RULES_CHANGED` listener; a failure is logged, never the person's."""
    for listener in RULES_CHANGED:
        try:
            listener(owner)
        except Exception:  # the rule is kept either way
            _logger.warning("rhythm: a rules-changed listener failed", exc_info=True)


def _state(value: str) -> str:
    return CHATTING if value == CHATTING else NOT


def _shown(rule: reach.Rule, tz: Any) -> dict[str, Any]:
    """A rule as the tool and Settings show it."""
    until = None
    if rule.expires_at is not None:
        # The moment it ends, in the person's zone ("today" ends at the next 00:00).
        until = datetime.fromtimestamp(rule.expires_at, tz).strftime("%Y-%m-%d %H:%M")
    return {
        "id": rule.id,
        "kind": rule.kind,
        "params": dict(rule.params),
        "said": rule.said,
        "until": until,
        "line": reach.line(rule, until=until),
    }
