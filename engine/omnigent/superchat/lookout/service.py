"""The Lookout (ADR 0038): one per person, wakes on signals, speaks through one run.

A wake, in order:

1. **Lease** the person's due wake (one replica at a time; a crash only delays it).
2. **Settle by code**: signals past ``stale_after`` only set their topic's "New" line; noise and
   muted ones are dropped (the hard filter, ADR 0040). The rest are matched to a topic (strong /
   medium / weak, common terms filtered, the topic's watch list); the match is only a hint.
3. **Judge** (ADR 0040, :mod:`~omnigent.superchat.lookout.judge`): one direct model call over the
   signals not judged yet scores each one (relevance, topic, urgency, why), knowing the person
   (memory pack, their own messages of the last day). Below the floor a signal never starts a
   message; it waits for the morning brief. The Judge's ``now`` makes it urgent for this wake;
   its topic is the signal's when code found none.
4. **Rhythm**: every ceiling lives in the rhythm capability (handed in by the composition root,
   :meth:`Lookout.set_rhythm`): the level (``off`` never speaks), the hard window (the person's
   quiet hours, else weekdays 8-18 in their zone), the daily cap of their level and one message
   per topic per 3 hours. ``now`` signals pass those ceilings only (``urgent=True``); ``normal``
   ones also need a good learned moment. A topic that may not speak now holds its signals until
   ``next_good_slot``.
5. **One run**: the signals left are claimed by a wake id and a ``lookout_wake`` job is queued in
   the work queue (:func:`~omnigent.superchat.lookout.run.launch_wake`). The run's Result is
   posted by code (:mod:`~omnigent.superchat.lookout.post`) with its outbox row, a card for the
   draft file it saved, and a ``sent`` event for the rhythm with the state the wake checked;
   "nothing worth saying" posts nothing. A failed, skipped or stuck run releases its signals.

The Lookout never wakes on an empty tray: its next wake is its earliest due signal, a held
signal's next chance, or a new signal (``add_signal`` moves it earlier).

The morning brief is a run of its own Automation (ADR 0037, amended): when it fires, the waiting
signals are claimed into it (:meth:`Lookout.claim_for_brief`) and its Result is posted the same
way (:meth:`Lookout.on_brief_result`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from omnigent.entities import OwnerPreferences
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.superchat.feature import list_mutes
from omnigent.superchat.lookout import judge as judging
from omnigent.superchat.lookout.judge import (
    HINT_REASON,
    HINT_STRENGTH,
    NO_MATCH,
    RELEVANCE_FLOOR,
    Verdict,
    verdict_of,
)
from omnigent.superchat.lookout.post import (
    denied_call,
    is_policy_text,
    post_message,
    result_text,
    run_error,
    saved_file_info,
)
from omnigent.superchat.lookout.prompt import (
    build_wake,
    parse_result,
    parse_statuses,
    signal_lines,
    topic_lines,
)
from omnigent.superchat.lookout.rules import (
    Item,
    WatchedTopic,
    common_terms,
    match,
    muted_for,
    noise_reason,
    norm_term,
    sender_match,
    term_hits,
    topic_muted,
    watched_topic,
)
from omnigent.superchat.lookout.run import (
    LOOKOUT_KIND,
    ensure_task,
    launch_wake,
)
from omnigent.superchat.lookout.signals import (
    JUDGE_KEY,
    MAIL,
    NOW,
    PROMISE,
    Signal,
    due_words,
    parse_due,
    promise_times,
    stale_after_for,
    zone,
)
from omnigent.superchat.lookout.store import SqlAlchemyLookoutStore, StoredSignal, Wake

_logger = logging.getLogger(__name__)

#: How often the ticker looks for due wakes (a cheap indexed read, never a model run).
TICK_SECONDS = 30.0
#: How long one replica holds a person's wake while it checks.
LEASE_SECONDS = 300
#: While a run is in flight the person is looked at again after this long (the run's end looks
#: sooner); also the delay before a released wake tries again.
RECHECK_SECONDS = 600
#: After a run ends, the next code check of what is still waiting.
AFTER_RUN_SECONDS = 60
#: A run claimed this long ago that never reported is released (the backstop).
STUCK_SECONDS = 2 * 3600
#: How often the ticker sweeps for stuck runs.
SWEEP_SECONDS = 600.0
#: A signal whose run failed this many times (an error, a policy block, a budget) is given up:
#: settled ``failed``, its topic marked "New".
MAX_ATTEMPTS = 2
#: How long to wait for a finished run's Result to be readable.
RESULT_WAIT_SECONDS = (0.0, 2.0, 5.0)
#: Signals one wake run reads; the urgent, the most relevant and the newest first.
MAX_PER_RUN = 12
#: The person's own messages in their Conversation the Judge reads: at most this old, the newest
#: first up to this many characters, from at most this many messages read.
CONTEXT_MAX_AGE_SECONDS = 24 * 3600
CONTEXT_MAX_CHARS = 2_000
CONTEXT_MAX_MESSAGES = 60
#: Wake ledger kinds.
WAKE, BRIEF = "wake", "brief"


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(fn, *args)


def _promise_id(entry: Mapping[str, Any]) -> str:
    """A promise line's id: its own when the topic writer gave one, else from its text."""
    given = str(entry.get("id") or "").strip()
    if given:
        return given[:64]
    return hashlib.sha256(str(entry.get("text") or "").encode()).hexdigest()[:12]


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _dated_promises(topic: Any) -> dict[str, tuple[Mapping[str, Any], str]]:
    """``promise id -> (line, due)`` of a topic's open lines that carry a date."""
    if topic is None or _field(topic, "status") not in (None, "open"):
        return {}
    found: dict[str, tuple[Mapping[str, Any], str]] = {}
    for entry in _field(topic, "open") or ():
        if isinstance(entry, Mapping) and parse_due(entry.get("due")) is not None:
            found[_promise_id(entry)] = (entry, str(entry["due"])[:10])
    return found


def new_line(signal: StoredSignal, today: Any = None) -> str:
    """The topic's "New" line a signal sets (stale, held past its time, or told), in plain words:
    "Send Dana the link · due today", never a raw date. *today* is the person's local day (UTC
    when not given)."""
    if signal.kind == PROMISE:
        day = today if today is not None else datetime.now(UTC).date()
        words = due_words(str(signal.detail.get("due") or ""), day)
        return f"{signal.subject} · {words}" if words else signal.subject
    if signal.kind == MAIL:
        return signal.subject
    return f"Updated: {signal.subject}"


def _direct(signal: StoredSignal) -> bool:
    """A source says the person is part of it (a mail thread they wrote in): worth a run's look
    even when it matches no topic (ADR 0039)."""
    return bool(signal.detail.get("direct"))


class Lookout:
    """Signals in, at most one message out per wake."""

    def __init__(
        self,
        *,
        store: SqlAlchemyLookoutStore,
        tasks: Any,
        conversations: Any,
        own_terms: Callable[[str], Sequence[str]] | None = None,
        judge_caller: Callable[[str, Any], Awaitable[judging.Caller | None]] | None = None,
        memory: Callable[[str], str | None] | None = None,
        voice: Callable[[str], str | None] | None = None,
    ) -> None:
        """
        :param store: The ``lookout_*`` tables.
        :param tasks: The scheduled task store (the hidden task, owner preferences).
        :param conversations: The conversation store.
        :param own_terms: ``owner -> terms`` of the person's own organisation (common terms).
        :param judge_caller: ``(owner, their Conversation or None) -> the Judge's model call``
            (the model their Conversation runs, else their Builder pick), or ``None`` when no
            model can be called.
        :param memory: ``owner -> memory pack`` (blocking; the memory capability's).
        :param voice: ``owner -> how the person writes`` (blocking; the memory capability's).
        """
        self.store = store
        self._tasks = tasks
        self._conversations = conversations
        self._own_terms = own_terms or _own_org_terms
        self._judge_caller = judge_caller or judging.resolve_caller
        self._memory = memory or _memory_pack
        self._voice = voice or _writing_voice
        self._list_topics: Callable[[str], Sequence[Any]] | None = None
        self._mark_new: Callable[[str, str, str, int], Any] | None = None
        self._set_status: Callable[..., Any] | None = None
        self._rhythm: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poke: asyncio.Event | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._pending: set[asyncio.Future[Any]] = set()

    def set_topic_seams(
        self,
        list_topics: Callable[[str], Sequence[Any]] | None,
        mark_new: Callable[[str, str, str, int], Any] | None,
        set_status: Callable[..., Any] | None = None,
    ) -> None:
        """The topics capability's readers and its status writer
        (``set_status(owner, key, line, prepared, message)``), handed in by the composition
        root."""
        self._list_topics, self._mark_new = list_topics, mark_new
        self._set_status = set_status

    def set_rhythm(self, rhythm: Any | None) -> None:
        """The rhythm capability (``good_moment``, ``next_good_slot``, ``record``), handed in by
        the composition root: it owns every ceiling on when the person hears from the Lookout."""
        self._rhythm = rhythm

    # ------------------------------------------------------------------ the entry point

    def _prefs(self, owner: str) -> OwnerPreferences:
        prefs = self._tasks.get_owner_preferences(owner) if self._tasks is not None else None
        return prefs or OwnerPreferences(user_id=owner)

    def add_signal(self, signal: Signal | Mapping[str, Any]) -> str:
        """Leave *signal* for its owner's Lookout (blocking; no model run). A ``stale_after`` of
        0 takes the kind's default in the person's zone.

        :returns: The signal's id.
        """
        if not isinstance(signal, Signal):
            signal = Signal(**dict(signal))
        owner = signal.owner or RESERVED_USER_LOCAL
        stale = signal.stale_after
        if stale <= 0:
            tz = zone(self._prefs(owner).timezone)
            stale = stale_after_for(signal.kind, signal.due_at, tz, urgency=signal.urgency or None)
        cleaned = Signal(
            owner=owner,
            kind=signal.kind,
            subject=signal.subject,
            dedupe_key=signal.dedupe_key,
            due_at=signal.due_at,
            stale_after=stale,
            topic_key=signal.topic_key,
            detail=signal.detail,
            conversation_id=signal.conversation_id,
            urgency=signal.urgency,
        ).cleaned()
        signal_id = self.store.add(cleaned)
        self._wake_soon()
        return signal_id

    def remove_signal(self, owner: str, dedupe_key: str) -> int:
        """Drop the owner's waiting signal(s) with *dedupe_key* (blocking)."""
        return self.store.remove(owner or RESERVED_USER_LOCAL, dedupe_key)

    def _today(self, owner: str) -> Any:
        """The person's local day (blocking: reads their zone)."""
        try:
            tz = zone(self._prefs(owner).timezone)
        except Exception:  # noqa: BLE001 - a "New" line in UTC words beats none
            tz = zone(None)
        return datetime.now(tz).date()

    def check_item(self, owner: str, fields: Mapping[str, Any]) -> dict[str, str]:
        """The code check a source runs before it pushes (blocking, no model): ``{"noise":
        why}``, or the topic the item matches ``{"topic_key", "strength", "reason"}`` (the
        topic matching rules, else a sender on a topic's watch list), or ``{}``.

        *fields*: ``source``, ``id``, ``subject``, ``excerpt``, ``sender``, ``people``, ``refs``,
        ``headers``, ``thread``, ``at``, ``from_self`` (all optional).
        """
        item = _fields_item(fields)
        noise = noise_reason(item)
        if noise is not None:
            return {"noise": noise}
        raw = self._list_topics(owner) if self._list_topics is not None else []
        topics = [t for t in (watched_topic(r) for r in raw or () if isinstance(r, Mapping)) if t]
        if not topics:
            return {}
        mutes = [m for m in list_mutes(owner) if isinstance(m, Mapping)]
        watched = sorted({t for t in (norm_term(x) for p in topics for x in p.terms) if t})
        try:
            own = list(self._own_terms(owner))
        except Exception:  # noqa: BLE001 - without them only the seeds and stoplist apply
            own = []
        stats = self.store.term_counts(owner, watched) if watched else {}
        common = common_terms(topics, own=own, stats=stats)
        found = match(item, topics, mutes, common) or sender_match(item, topics, mutes)
        if found is None:
            return {}
        return {"topic_key": found.topic_key, "strength": found.strength, "reason": found.reason}

    def _wake_soon(self) -> None:
        """Tell the ticker a signal arrived (thread-safe; a no-op before it started)."""
        loop, poke = self._loop, self._poke
        if loop is None or poke is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(poke.set)

    def on_rules_changed(self, owner: str) -> None:
        """The person changed how they may be reached (a reach rule added or lifted, through
        the rhythm): when anything waits, look again now instead of at the held slot
        (blocking). A running wake keeps its lease; the next one reads the new rules."""
        if not self.store.waiting(owner):
            return
        self.store.wake_by(owner, int(time.time()))
        self._wake_soon()

    # ------------------------------------------------------------------ promises (topics)

    def on_topic_write(self, owner: str, before: Any, after: Any) -> None:
        """Keep the topic's promise signals in step with its dated open lines (blocking).

        Keyed ``promise:<topic>:<id>``: rewording keeps the reminder, a changed date moves it, a
        closed promise (or topic) removes it. An unchanged promise is left alone, so a reminder
        already told is not told again.
        """
        key = str(_field(after, "key") or _field(before, "key") or "")
        if not key:
            return
        was, now_dated = _dated_promises(before), _dated_promises(after)
        for promise_id in set(was) - set(now_dated):
            self.remove_signal(owner, f"promise:{key}:{promise_id}")
        tz = zone(self._prefs(owner).timezone)
        instant = int(time.time())
        for promise_id, (entry, due) in now_dated.items():
            if was.get(promise_id, (None, None))[1] == due:
                continue
            day = parse_due(due)
            assert day is not None
            due_at, stale_after = promise_times(day, tz)
            if stale_after <= instant:
                continue
            self.add_signal(
                Signal(
                    owner=owner,
                    kind=PROMISE,
                    subject=str(entry.get("text") or ""),
                    dedupe_key=f"promise:{key}:{promise_id}",
                    due_at=due_at,
                    stale_after=stale_after,
                    topic_key=key,
                    detail={"due": due, "promise_kind": entry.get("kind"), "at": instant},
                )
            )

    # ------------------------------------------------------------------ the ticker

    async def start(self) -> None:
        """Start the ticker that wakes due Lookouts."""
        self._loop = asyncio.get_running_loop()
        self._poke = asyncio.Event()
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick(), name="lookout-ticker")

    async def shutdown(self) -> None:
        """Stop the ticker."""
        if self._ticker is not None:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
        self._ticker = None

    async def _tick(self) -> None:
        from omnigent.db.db_models import workspace_scope

        last_sweep = 0.0
        while True:
            assert self._poke is not None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._poke.wait(), timeout=TICK_SECONDS)
            self._poke.clear()
            try:
                if time.monotonic() - last_sweep >= SWEEP_SECONDS:
                    last_sweep = time.monotonic()
                    await self._sweep()
                due = await asyncio.to_thread(self.store.due_owners, int(time.time()))
                for workspace, owner in due:
                    with workspace_scope(workspace):
                        try:
                            await self.wake(owner)
                        except Exception:
                            _logger.exception("lookout: the wake of %s failed", owner)
                            await asyncio.to_thread(
                                self.store.set_next_wake, owner, int(time.time()) + RECHECK_SECONDS
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("lookout: the ticker failed")

    async def _sweep(self) -> None:
        """Release runs that never reported (their signals wait again)."""
        from omnigent.db.db_models import workspace_scope

        stuck = await asyncio.to_thread(self.store.stuck_wakes, int(time.time()) - STUCK_SECONDS)
        for workspace, wake_id in stuck:
            with workspace_scope(workspace):
                wake = await asyncio.to_thread(self.store.get_wake, wake_id)
                if wake is not None:
                    await asyncio.to_thread(self._fail, wake, "its run never reported")

    # ------------------------------------------------------------------ one wake

    async def wake(self, owner: str) -> str:
        """Check the owner's due signals and speak, hold or settle them.

        :returns: What happened, for the log: ``launched``, ``held:<why>``, ``empty`` ...
        """
        now = int(time.time())
        if not await asyncio.to_thread(self.store.lease, owner, now, LEASE_SECONDS):
            return "busy"
        outcome, next_at = await self._wake(owner, now)
        await asyncio.to_thread(self.store.set_next_wake, owner, next_at)
        _logger.info(
            "lookout for %s: %s; next wake %s",
            owner,
            outcome,
            datetime.fromtimestamp(next_at, UTC).isoformat() if next_at else "on a new signal",
        )
        return outcome

    async def _wake(self, owner: str, now: int) -> tuple[str, int | None]:
        """The wake's body: ``(outcome, next wake or None)``."""
        waiting = await asyncio.to_thread(self.store.waiting, owner)
        if not waiting:
            return "empty", None  # never wakes on an empty tray; the run's end looks again
        if any(w.kind == WAKE for w in await asyncio.to_thread(self.store.in_flight, owner)):
            return "run_in_flight", now + RECHECK_SECONDS
        prefs = await asyncio.to_thread(self._prefs, owner)
        topics = await self._topics(owner)
        due = [s for s in waiting if s.due_at <= now]
        later = [s for s in waiting if s.due_at > now]
        live = await self._settle(owner, due, topics, now)
        judged = await self._judge(owner, live, topics)
        candidates = [s for s in judged if _relevance(s) >= RELEVANCE_FLOOR]
        # Below the floor a signal waits for the brief; one the brief never claims is looked at
        # again when it goes stale, which only marks its topic "New".
        low = [s for s in judged if _relevance(s) < RELEVANCE_FLOOR]

        def next_from(held: Sequence[StoredSignal], *also: int | None) -> int | None:
            times = [s.due_at for s in later] + [s.stale_after for s in [*held, *low]]
            times += [t for t in also if t is not None]
            return max(min(times), now + 1) if times else None

        if not candidates:
            return ("waiting_for_brief" if low else "nothing_due"), next_from([])
        if self._rhythm is None:
            _logger.warning("lookout: no rhythm wired; %s holds its signals", owner)
            return "held:no_rhythm", next_from(candidates, now + RECHECK_SECONDS)
        tz = zone(prefs.timezone)
        at = datetime.fromtimestamp(now, tz)
        state = await self._state(owner)
        free: list[StoredSignal] = []
        reopen: list[int] = []
        for topic_key in sorted({s.topic_key or "" for s in candidates}):
            on_topic = [s for s in candidates if (s.topic_key or "") == topic_key]
            urgent_topic = any(s.urgency == NOW for s in on_topic)
            # Who each signal comes from, for the person's only-for and mute rules (the rhythm).
            senders = [str(s.detail.get("sender") or "") or None for s in on_topic]
            ok = await asyncio.to_thread(
                lambda k=topic_key, u=urgent_topic, p=senders: self._rhythm.good_moment(
                    owner, at, state, topic_key=k, urgent=u, senders=p
                )
            )
            if ok:
                free.extend(on_topic)
                continue
            slot = await asyncio.to_thread(
                lambda k=topic_key, u=urgent_topic, p=senders: self._rhythm.next_good_slot(
                    owner, at, state, topic_key=k, urgent=u, senders=p
                )
            )
            reopen.append(max(int(slot.timestamp()), now + 1))
        held = [s for s in candidates if s not in free]
        if not free:
            return "held:rhythm", next_from(candidates, min(reopen) if reopen else None)
        urgent = any(s.urgency == NOW for s in free)
        # Only what still waits sets the next wake; the run's end looks again itself.
        return await self._launch(owner, prefs, free, urgent, now, state), next_from(held, *reopen)

    async def _topics(self, owner: str) -> list[WatchedTopic]:
        if self._list_topics is None:
            return []
        try:
            raw = await _call(self._list_topics, owner) or []
        except Exception:
            _logger.exception("lookout: reading the topics of %s failed", owner)
            return []
        return [t for t in (watched_topic(r) for r in raw if isinstance(r, Mapping)) if t]

    async def _mark(self, owner: str, signals: Sequence[StoredSignal]) -> None:
        """Give each signal's topic its "New" line (the newest per topic wins)."""
        if self._mark_new is None:
            return
        today = await _call(self._today, owner)
        for signal in sorted(signals, key=lambda s: s.created_at):
            if not signal.topic_key:
                continue
            try:
                await _call(
                    self._mark_new,
                    owner,
                    signal.topic_key,
                    new_line(signal, today),
                    signal.created_at,
                )
            except Exception:
                _logger.exception("lookout: marking %s failed for %s", signal.topic_key, owner)

    async def _set_statuses(
        self,
        owner: str,
        signals: Sequence[StoredSignal],
        statuses: Mapping[str, str],
        *,
        item_id: str,
        saved: tuple[str, str] | None,
        message: str,
    ) -> None:
        """After a posted wake: each topic it covered gets the status line the run wrote for it,
        the message's first sentence as where it stands (the pick card never shows an older
        summary) and, when the run saved a draft, a pointer to that draft so Home and the pick
        card can lead with it."""
        if self._set_status is None:
            return
        keys = list(dict.fromkeys(s.topic_key for s in signals if s.topic_key))
        prepared = (
            {
                "item_id": item_id,
                "artifact_id": saved[0],
                "title": saved[1],
                "line": message,
                "at": int(time.time()),
            }
            if saved
            else None
        )
        for key in keys:
            try:
                await _call(
                    self._set_status, owner, key, statuses.get(key.lower()), prepared, message
                )
            except Exception:
                _logger.exception("lookout: the status of %s failed for %s", key, owner)

    async def _settle(
        self, owner: str, due: Sequence[StoredSignal], topics: Sequence[WatchedTopic], now: int
    ) -> list[StoredSignal]:
        """The code check over the due signals (the hard filter, ADR 0040): stale ones only
        mark their topic; noise and muted ones are settled; the rest are kept for the Judge, a
        matched one with its topic and every one with the code's hint (matched once: the hint
        is kept on the signal, so a held signal is not counted into the term stats again).

        :returns: The signals still worth the Judge's look.
        """
        stale = [s for s in due if s.stale_after <= now]
        fresh = [s for s in due if s.stale_after > now]
        mutes: list[Mapping[str, Any]] = []
        try:
            mutes = [m for m in await _call(list_mutes, owner) if isinstance(m, Mapping)]
        except Exception:
            _logger.warning("lookout: mutes of %s unreadable; none applied", owner, exc_info=True)
        kept: list[StoredSignal] = []
        drop: dict[str, list[str]] = {"noise": [], "muted": []}
        to_match = [
            (s, _item(s))
            for s in [*stale, *fresh]
            if not s.topic_key and HINT_REASON not in s.detail
        ]
        common = await self._common_terms(owner, [i for _, i in to_match], topics)
        matched: dict[str, StoredSignal] = {}
        for signal, item in to_match:
            if noise_reason(item) is not None:
                drop["noise"].append(signal.id)
                continue
            found = match(item, topics, mutes, common)
            hint = (
                {HINT_STRENGTH: found.strength, HINT_REASON: found.reason}
                if found is not None
                else {HINT_REASON: NO_MATCH}
            )
            await asyncio.to_thread(
                lambda i=signal.id, h=hint, k=found.topic_key if found else None: (
                    self.store.annotate(i, h, topic_key=k)
                )
            )
            matched[signal.id] = replace(
                signal,
                topic_key=found.topic_key if found else None,
                detail={**signal.detail, **hint},
            )
        resolved = [matched.get(s.id, s) for s in [*stale, *fresh]]
        for signal in resolved:
            if signal.id in drop["noise"]:
                continue
            if signal.topic_key and topic_muted(mutes, signal.topic_key):
                drop["muted"].append(signal.id)
                continue
            if signal.stale_after <= now:
                continue
            kept.append(signal)
        stale_resolved = [
            s
            for s in resolved
            if s.stale_after <= now and s.topic_key and s.id not in drop["muted"]
        ]
        await self._mark(owner, stale_resolved)
        await asyncio.to_thread(self.store.settle, [s.id for s in stale], "stale", now=now)
        for outcome, ids in drop.items():
            await asyncio.to_thread(self.store.settle, ids, outcome, now=now)
        return kept

    async def _judge(
        self, owner: str, live: Sequence[StoredSignal], topics: Sequence[WatchedTopic]
    ) -> list[StoredSignal]:
        """Judge the live signals not judged yet in one model call (ADR 0040) and store each
        verdict on its signal; a topic the Judge gives a signal without one becomes its topic,
        then the person's mutes apply to it as to a code match.

        :returns: The live signals, each with its verdict, a ``now`` verdict as its urgency for
            this wake; muted ones are settled and left out.
        """
        fresh = [s for s in live if verdict_of(s) is None]
        verdicts: dict[str, Verdict] = {}
        if fresh:
            conversation = await self._conversation(owner)
            memory, context = await asyncio.gather(
                asyncio.to_thread(self._memory, owner), self._context(conversation)
            )
            caller = await self._judge_caller(owner, conversation)
            verdicts, how = await judging.judge(
                fresh, topics, memory=memory, context=context, caller=caller
            )
            for signal in fresh:
                verdict = verdicts[signal.id]
                await asyncio.to_thread(
                    lambda i=signal.id, v=verdict, k=signal.topic_key: self.store.annotate(
                        i, {JUDGE_KEY: v.as_detail()}, topic_key=None if k else v.topic
                    )
                )
            _logger.info(
                "lookout judge for %s on %s (%s, memory %s, context %s): %s",
                owner,
                getattr(caller, "model", None) or ("no model" if caller is None else "a model"),
                how,
                "yes" if memory else "none",
                "yes" if context else "none",
                "; ".join(
                    f"{s.id[:8]} {s.kind} {v.relevance}/{v.urgency}"
                    f"{' ' + v.topic if v.topic else ''} [{v.by}] {v.why}"
                    for s in fresh
                    for v in (verdicts[s.id],)
                ),
            )
        mutes: list[Mapping[str, Any]] = []
        try:
            mutes = [m for m in await _call(list_mutes, owner) if isinstance(m, Mapping)]
        except Exception:
            _logger.warning("lookout: mutes of %s unreadable; none applied", owner, exc_info=True)
        out: list[StoredSignal] = []
        muted: list[str] = []
        for signal in live:
            verdict = verdicts.get(signal.id) or verdict_of(signal)
            assert verdict is not None
            topic = signal.topic_key or verdict.topic
            if (
                topic
                and not signal.topic_key
                and muted_for(mutes, topic, signal.detail.get("sender"))
            ):
                muted.append(signal.id)
                continue
            out.append(
                replace(
                    signal,
                    topic_key=topic,
                    urgency=NOW if verdict.urgency == "now" else signal.urgency,
                    detail={**signal.detail, JUDGE_KEY: verdict.as_detail()},
                )
            )
        await asyncio.to_thread(self.store.settle, muted, "muted")
        return out

    async def _conversation(self, owner: str) -> Any | None:
        """The person's Conversation, or ``None`` (unreadable: logged)."""
        try:
            conversation_id = await asyncio.to_thread(self._conversation_id, owner)
            if conversation_id is None:
                return None
            return await asyncio.to_thread(self._conversations.get_conversation, conversation_id)
        except Exception:
            _logger.warning("lookout: the Conversation of %s unreadable", owner, exc_info=True)
            return None

    async def _context(self, conversation: Any | None) -> str | None:
        """What the person said in their Conversation over the last day: their own messages
        (never a runtime notice), oldest first, the newest kept up to
        :data:`CONTEXT_MAX_CHARS`; ``None`` when they said nothing.

        Their words, not the fold's summary: the fold runs only when the context fills, so on a
        large window a whole day passes without one.
        """
        from omnigent.entities.conversation import is_system_notice

        if conversation is None:
            return None
        try:
            page = await asyncio.to_thread(
                lambda: self._conversations.list_items(
                    conversation.id, limit=CONTEXT_MAX_MESSAGES, order="desc", type="message"
                )
            )
        except Exception:
            _logger.warning("lookout: messages of %s unreadable", conversation.id, exc_info=True)
            return None
        since = time.time() - CONTEXT_MAX_AGE_SECONDS
        lines: list[str] = []
        used = 0
        for item in page.data:
            data = item.data
            if item.created_at < since:
                break
            if getattr(data, "role", None) != "user" or is_system_notice(data):
                continue
            text = " ".join(
                " ".join(b["text"] for b in data.content if isinstance(b.get("text"), str)).split()
            )
            if not text:
                continue
            line = f"- {text}"
            if used + len(line) + 1 > CONTEXT_MAX_CHARS:
                if not lines:
                    lines.append(line[: CONTEXT_MAX_CHARS - 1] + "…")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(reversed(lines)) or None

    async def _common_terms(
        self, owner: str, items: Sequence[Item], topics: Sequence[WatchedTopic]
    ) -> dict[str, str]:
        """The owner's common terms, after counting this wake's unmatched items in."""
        watched = sorted({t for t in (norm_term(x) for p in topics for x in p.terms) if t})
        if not watched:
            return {}
        hits = [term_hits(i, watched) for i in items]
        stored = await asyncio.to_thread(self.store.term_counts, owner, watched)
        today = {term: (sum(term in h for h in hits), len(hits)) for term in watched}
        stats = {
            term: (
                stored.get(term, (0, 0))[0] + today[term][0],
                stored.get(term, (0, 0))[1] + today[term][1],
            )
            for term in watched
        }
        if items:
            await asyncio.to_thread(self.store.add_terms, owner, today)
        try:
            own = list(await asyncio.to_thread(self._own_terms, owner))
        except Exception:
            _logger.warning("lookout: own organisation of %s unreadable", owner, exc_info=True)
            own = []
        return common_terms(topics, own=own, stats=stats)

    async def _state(self, owner: str) -> str:
        """``chatting`` while a turn of the person's Conversation runs (its stored live
        status), else ``not`` (ADR 0038, "Two states")."""
        conversation_id = await asyncio.to_thread(self._conversation_id, owner)
        if conversation_id is None:
            return "not"
        conv = await asyncio.to_thread(self._conversations.get_conversation, conversation_id)
        return (
            "chatting" if getattr(conv, "live_status", None) in ("running", "waiting") else "not"
        )

    def _conversation_id(self, owner: str) -> str | None:
        """The person's Conversation: as a source last named it, else found by its label."""
        from omnigent.superchat.feature import is_super_chat
        from omnigent.superchat.muse import MUSE_LABEL_KEY

        known = self.store.conversation_of(owner)
        if known:
            conv = self._conversations.get_conversation(known)
            if conv is not None and is_super_chat(conv.labels) and not conv.archived:
                return known
        for session_id in self.store.conversations(MUSE_LABEL_KEY):
            found = self._conversations.get_session_owner(session_id, owner_only=True)
            if found == owner or (owner == RESERVED_USER_LOCAL and found is None):
                self.store.remember_conversation(owner, session_id)
                return session_id
        return None

    async def _launch(
        self,
        owner: str,
        prefs: OwnerPreferences,
        signals: Sequence[StoredSignal],
        urgent: bool,
        now: int,
        state: str,
    ) -> str:
        """Claim *signals* into a wake and queue its run (*state*: what the rhythm checked)."""
        conversation_id = await asyncio.to_thread(self._conversation_id, owner)
        conv = (
            await asyncio.to_thread(self._conversations.get_conversation, conversation_id)
            if conversation_id
            else None
        )
        if conv is None or conv.agent_id is None:
            return "held:no_conversation"
        user_id = None if owner == RESERVED_USER_LOCAL else owner
        task = await asyncio.to_thread(
            ensure_task, self._tasks, conv, user_id, prefs.timezone or "UTC"
        )
        chosen = sorted(
            signals, key=lambda s: (-(s.urgency == NOW), -_relevance(s), -s.created_at)
        )[:MAX_PER_RUN]
        wake_id, claimed = await asyncio.to_thread(
            lambda: self.store.claim(
                owner=owner,
                kind=WAKE,
                signal_ids=[s.id for s in chosen],
                urgency="now" if urgent else "today",
                topic_keys=[s.topic_key for s in chosen if s.topic_key],
                conversation_id=conv.id,
                scheduled_task_id=task.id,
                now=now,
                state=state,
            )
        )
        if not claimed:
            await asyncio.to_thread(self.store.release, wake_id)
            return "nothing_claimed"
        try:
            started = await asyncio.to_thread(launch_wake, task, wake_id)
        except Exception:
            _logger.exception("lookout: launching wake %s failed", wake_id)
            started = False
        if not started:
            await asyncio.to_thread(self.store.release, wake_id)
            return "launch_refused"
        return f"launched {claimed} signal(s)"

    # ------------------------------------------------------------------ the queue job

    async def run_job(self, job: Any) -> Any:
        """The work queue's handler for a ``lookout_wake`` job: run the wake's hidden task
        through the parent-bound fire path while its signals are still claimed.

        A wake released meanwhile (its earlier attempt failed, the sweep gave up on it) runs
        nothing. A job that fails, or ends with nothing running and its wake still claimed,
        releases the wake's signals so they wait again.

        :returns: The run's session id (the job holds the owner's slot until its turn ends), a
            ``Defer``, or ``None``.
        """
        from omnigent.server.scheduled.helper_fire import run_fire_job

        wake_id = str(job.payload.get("wake_id") or job.key)
        wake = await asyncio.to_thread(self.store.get_wake, wake_id)
        if wake is None or wake.status != "claimed":
            _logger.info(
                "lookout: wake %s is %s; its job starts nothing",
                wake_id,
                wake.status if wake else "gone",
            )
            return None
        try:
            result = await run_fire_job(job)
        except Exception:
            await asyncio.to_thread(self._fail, wake, "its queue job failed")
            raise
        if result is None:
            await asyncio.to_thread(self._release, wake, "nothing was started")
        return result

    def _release(self, wake: Wake, why: str) -> None:
        """Release a still-claimed wake whose run never started (a gate, a skip): its signals
        wait again, no attempt counted (blocking)."""
        if self.store.release(wake.id):
            _logger.info("lookout: wake %s of %s released: %s", wake.id, wake.owner, why)
            self._look_again(wake.owner)

    def _fail(self, wake: Wake, why: str) -> None:
        """The wake's run failed (an error, no Result, a Result that is a policy notice): post
        nothing, release its signals for a retry at the next wake, and give up on a signal after
        :data:`MAX_ATTEMPTS` (settled ``failed``, its topic marked "New") (blocking)."""
        given_up = self.store.fail(wake.id, max_attempts=MAX_ATTEMPTS)
        if given_up is None:
            return
        _logger.warning(
            "lookout: %s run %s of %s failed (%s); nothing posted, signals released%s",
            wake.kind,
            wake.id,
            wake.owner,
            why,
            f", {len(given_up)} given up" if given_up else "",
        )
        if given_up and self._mark_new is not None:
            today = self._today(wake.owner)
            for signal in given_up:
                if signal.topic_key:
                    try:
                        self._mark_new(
                            wake.owner,
                            signal.topic_key,
                            new_line(signal, today),
                            signal.created_at,
                        )
                    except Exception:
                        _logger.exception("lookout: marking %s failed", signal.topic_key)
        self._look_again(wake.owner)

    def _look_again(self, owner: str) -> None:
        """After a run ended: the next wake is the earliest waiting signal (soon at most), or
        none on an empty tray (blocking)."""
        waiting = self.store.waiting(owner)
        now = int(time.time())
        at = max(min(s.due_at for s in waiting), now + AFTER_RUN_SECONDS) if waiting else None
        self.store.set_next_wake(owner, at)

    # ------------------------------------------------------------------ the run's two ends

    def run_brief(self, task: Any) -> str | None:
        """The fire path's hook for a ``lookout_wake`` run: its opening message (blocking)."""
        wake = self.store.wake_for_task(task.id, run_conversation_id=None)
        signals = self.store.claimed(wake.id) if wake is not None else []
        owner = task.user_id or RESERVED_USER_LOCAL
        listed = [
            t
            for t in (
                watched_topic(r)
                for r in (self._list_topics(owner) if self._list_topics else [])
                if isinstance(r, Mapping)
            )
            if t
        ]
        keys = {s.topic_key for s in signals}
        titles = {t.key: t.title for t in listed}
        return build_wake(
            signal_lines(signals, titles) or "(nothing)",
            topic_lines([t for t in listed if t.key in keys]),
            urgent=wake is not None and wake.urgency == "now",
            memory=self._memory(owner),
            voice=self._voice(owner),
        )

    def claim_for_brief(self, task: Any) -> str | None:
        """The brief's hook when its run starts: claim the signals waiting now into it
        (blocking), the ones the Judge put below the floor included (they waited for it). Stale
        ones, and ones the Lookout has not checked yet, are left for the Lookout.

        :returns: One line per claimed signal, or ``None`` when none waits.
        """
        owner = task.user_id or RESERVED_USER_LOCAL
        now = int(time.time())
        waiting = [
            s
            for s in self.store.waiting(owner)
            if s.due_at <= now < s.stale_after
            and (s.topic_key or _direct(s) or verdict_of(s) is not None)
        ]
        wake_id, claimed = self.store.claim(
            owner=owner,
            kind=BRIEF,
            signal_ids=[s.id for s in waiting],
            urgency="brief",
            topic_keys=[s.topic_key for s in waiting if s.topic_key],
            conversation_id=task.parent_session_id,
            scheduled_task_id=task.id,
            now=now,
        )
        if not claimed:
            return None
        listed = self._list_topics(owner) if self._list_topics else []
        titles = {str(_field(t, "key")): str(_field(t, "title")) for t in listed}
        return signal_lines(self.store.claimed(wake_id), titles)

    def on_run_result(self, task: Any, run_conversation_id: str | None, status: str) -> None:
        """The fire path's report on a ``lookout_wake`` or brief run (blocking, never slow).

        ``running`` binds the run to its wake; ``skipped`` or ``failed`` releases the signals
        (they wait again); ``succeeded`` posts the Result on the event loop.
        """
        wake = self.store.wake_for_task(task.id, run_conversation_id=run_conversation_id)
        if wake is None:
            if task.kind != LOOKOUT_KIND and status == "succeeded" and run_conversation_id:
                # A brief run that claimed nothing (its wake was never opened): still posted.
                wake_id, _ = self.store.claim(
                    owner=task.user_id or RESERVED_USER_LOCAL,
                    kind=BRIEF,
                    signal_ids=[],
                    urgency="brief",
                    topic_keys=[],
                    conversation_id=task.parent_session_id,
                    scheduled_task_id=task.id,
                )
                wake = self.store.get_wake(wake_id)
            if wake is None:
                return
        if status == "running":
            if run_conversation_id:
                self.store.bind_run(wake.id, run_conversation_id)
            return
        unpriced = False
        if status in ("succeeded", "failed") and run_conversation_id:
            unpriced = self._log_cost(wake, task, run_conversation_id)
        if status == "succeeded" and run_conversation_id and not unpriced:
            self._schedule(self._post(wake, task, run_conversation_id))
            return
        if status == "failed" or unpriced:
            self._fail(wake, "a model with no price" if unpriced else "the run failed")
        else:
            self._release(wake, f"the run was {status}")

    def _log_cost(self, wake: Wake, task: Any, run_conversation_id: str) -> bool:
        """Log the run's spend (no per-run cap since ADR 0040; the run record and the model-call
        logs show it).

        :returns: Whether the run is capped and spent tokens on a model with no price: its
            policy refused the run's work (``unattended``), so its Result is not posted
            and its signals are released at once. Only a task still carrying a budget can be;
            an uncapped run is never refused and always posts.
        """
        try:
            conv = self._conversations.get_conversation(run_conversation_id)
            usage = getattr(conv, "session_usage", None) or {}
            priced = "total_cost_usd" in usage
            spent = bool(
                usage.get("input_tokens")
                or usage.get("output_tokens")
                or usage.get("total_tokens")
            )
            cost = float(usage.get("total_cost_usd") or 0.0)
        except Exception:  # noqa: BLE001 - a log line only
            return False
        if spent and not priced and getattr(task, "max_cost_usd", None) is not None:
            _logger.warning(
                "lookout: %s run %s of %s used a model with no price; it was stopped and "
                "its signals are released",
                wake.kind,
                wake.id,
                wake.owner,
            )
            return True
        _logger.info(
            "lookout: %s run %s of %s cost %s",
            wake.kind,
            wake.id,
            wake.owner,
            f"{cost:.4f} USD" if priced else "an unpriced amount",
        )
        return False

    def _schedule(self, coro: Any) -> None:
        """Run *coro* on the engine's loop from any thread, in the caller's workspace."""
        from omnigent.db.db_models import current_workspace_id, workspace_scope

        workspace = current_workspace_id()

        async def scoped() -> None:
            with workspace_scope(workspace):
                await coro

        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            _logger.warning("lookout: no event loop; a run's Result waits for the sweep")
            return
        future = asyncio.run_coroutine_threadsafe(scoped(), loop)
        self._pending.add(future)  # type: ignore[arg-type]
        future.add_done_callback(self._pending.discard)  # type: ignore[arg-type]

    async def _sent(self, wake: Wake, item_id: str) -> None:
        """Tell the rhythm the message went out, in the state the wake checked (the brief's
        state is read now: it was never checked)."""
        if self._rhythm is None:
            return
        state = wake.state or (await self._state(wake.owner) if wake.kind == BRIEF else "not")
        try:
            await asyncio.to_thread(
                lambda: self._rhythm.record(
                    wake.owner,
                    item_id,
                    "sent",
                    datetime.now(UTC),
                    state=state,
                    topic_keys=list(wake.topic_keys),
                )
            )
        except Exception:  # bookkeeping never unsays the message
            _logger.exception("lookout: recording sent for %s failed", item_id)

    async def _post(self, wake: Wake, task: Any, run_conversation_id: str) -> None:
        """Read the run's Result and post it (or nothing), then settle the wake's signals."""
        found: tuple[str, str] | None = None
        for delay in RESULT_WAIT_SECONDS:
            if delay:
                await asyncio.sleep(delay)
            found = await asyncio.to_thread(result_text, self._conversations, run_conversation_id)
            if found:
                break
        text, agent = found if found else (None, "assistant")
        # The run fails on its Result alone: none (or an error cut the run short) or one carrying
        # a policy's words. A refused call on the way, or a read whose output quotes a notice,
        # is only logged; the run's own record keeps it (ADR 0038, amended).
        if text is None:
            blocked: str | None = "the run left no Result"
        else:
            blocked = await asyncio.to_thread(run_error, self._conversations, run_conversation_id)
        if blocked is None and is_policy_text(text):
            blocked = "its Result is a policy notice"
        if blocked is not None:
            await asyncio.to_thread(self._fail, wake, blocked)
            return
        denied = await asyncio.to_thread(denied_call, self._conversations, run_conversation_id)
        if denied:
            _logger.info(
                "lookout: %s run %s of %s saw a policy notice in a tool output (%s); "
                "its Result is posted",
                wake.kind,
                wake.id,
                wake.owner,
                denied,
            )
        if wake.kind == WAKE:
            value, message = parse_result(text)
        else:
            # The brief's Result is its line in the chat, always posted (ADR 0037).
            value, message = "brief", (text or "").strip() or None
        signals = await asyncio.to_thread(self.store.claimed, wake.id)
        item_id: str | None = None
        conversation_id = wake.conversation_id or task.parent_session_id
        if message and conversation_id:
            topic = next((s.topic_key for s in signals if s.topic_key), None)
            # The draft (or the brief's page) the run saved: shown as a card on the message.
            saved = await asyncio.to_thread(
                saved_file_info, self._conversations, run_conversation_id
            )
            artifact_id = saved[0] if saved else None
            try:
                item_id = await asyncio.to_thread(
                    lambda: post_message(
                        self._conversations,
                        conversation_id=conversation_id,
                        owner=wake.owner,
                        wake_id=wake.id,
                        text=message,
                        agent=agent,
                        kind=task.kind or LOOKOUT_KIND,
                        urgency=wake.urgency,
                        topic_key=topic if wake.kind == WAKE else None,
                        artifact_id=artifact_id,
                    )
                )
            except Exception:
                _logger.exception("lookout: posting wake %s failed", wake.id)
                await asyncio.to_thread(self._fail, wake, "posting failed")
                return
            await self._sent(wake, item_id)
            if wake.kind == WAKE:
                await self._set_statuses(
                    wake.owner,
                    signals,
                    parse_statuses(text),
                    item_id=item_id,
                    saved=saved,
                    message=message,
                )
        await self._mark(wake.owner, signals)
        await asyncio.to_thread(lambda: self.store.finish(wake.id, item_id=item_id))
        _logger.info(
            "lookout: %s wake %s of %s: kind_of_value=%s, %s",
            wake.kind,
            wake.id,
            wake.owner,
            value,
            f"posted {item_id}" if item_id else "nothing worth saying",
        )
        if wake.kind == WAKE:
            await asyncio.to_thread(self._look_again, wake.owner)


def _item(signal: StoredSignal) -> Item:
    """A signal as the matching rules read it."""
    detail = signal.detail

    def strings(value: Any) -> tuple[str, ...]:
        return tuple(str(v) for v in value if v) if isinstance(value, (list, tuple)) else ()

    headers = detail.get("headers")
    return Item(
        source=str(detail.get("source") or signal.kind),
        id=str(detail.get("item_id") or signal.id),
        title=signal.subject,
        text_excerpt=str(detail.get("excerpt") or ""),
        at=int(detail.get("at") or signal.created_at),
        thread_ref=detail.get("thread"),
        sender=detail.get("sender"),
        people=strings(detail.get("people")),
        refs=strings(detail.get("refs")),
        headers=headers if isinstance(headers, Mapping) else {},
        from_self=bool(detail.get("from_self")),
    )


def _fields_item(fields: Mapping[str, Any]) -> Item:
    """A source's item fields (:meth:`Lookout.check_item`) as the matching rules read them."""

    def strings(value: Any) -> tuple[str, ...]:
        return tuple(str(v) for v in value if v) if isinstance(value, (list, tuple)) else ()

    headers = fields.get("headers")
    return Item(
        source=str(fields.get("source") or ""),
        id=str(fields.get("id") or ""),
        title=str(fields.get("subject") or ""),
        text_excerpt=str(fields.get("excerpt") or ""),
        at=int(fields.get("at") or 0),
        thread_ref=fields.get("thread"),
        sender=fields.get("sender"),
        people=strings(fields.get("people")),
        refs=strings(fields.get("refs")),
        headers=headers if isinstance(headers, Mapping) else {},
        from_self=bool(fields.get("from_self")),
    )


def _relevance(signal: StoredSignal) -> int:
    """The Judge's relevance of *signal* (its hint's fallback before it is judged)."""
    verdict = verdict_of(signal)
    return (verdict or judging.hint_verdict(signal)).relevance


def _memory_pack(owner: str) -> str | None:
    """The person's memory pack (ADR 0040), from the memory capability."""
    from omnigent.memory.pack import memory_pack

    return memory_pack(owner)


def _writing_voice(owner: str) -> str | None:
    """How the person writes, from memory (ADR 0040)."""
    from omnigent.memory.pack import writing_voice

    return writing_voice(owner)


def _own_org_terms(owner: str) -> list[str]:
    """The person's own organisation and domains, from memory and their address."""
    from omnigent.memory.identity import own_org_terms

    return own_org_terms(owner)
