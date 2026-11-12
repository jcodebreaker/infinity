"""The quiet-moment pass: keep today's daily note (and memory) current while the person is away.

When a top-level Super Chat goes quiet (default 20 minutes) after a substantial exchange (at
least :data:`MIN_TURNS` user messages since the last pass), one bounded ``notes`` Helper reads
the recent conversation, updates today's note with ``daily_note_update``, records lasting
facts with ``memory_remember`` and keeps the person's topics with ``topics_update`` (ADR 0035).
It is an ad-hoc Helper (``start_adhoc_helper``): its Result never wakes the Conversation, so the
person is never messaged.

Gating reuses the scheduled-run rules: nothing runs while the owner's proactivity is ``off`` or
inside their quiet hours, and at most :data:`MAX_PASSES_PER_DAY` passes run per owner per local
day. Each day is finalized once with one more pass in the local evening (a day that ended
unfinalized is only closed). Idleness is re-derived on every tick from the stored messages and
the note's own counters, so a restart never loses a due pass.

A pass takes the note's lease when it starts and sets its done marker (``finalized_at``,
``dreamed_at``, ``people_at``) only once its Helper finished and wrote content. A pass that
crashed or wrote nothing keeps the lease and is retried when the lease expires.

Every pass is a job in the work queue (ADR 0038, kind ``quiet_moment``): it starts its Helper
when the owner and the engine have room, and holds the owner's slot until the Helper's turn ends.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from omnigent.context.state_file import STALE_MARK
from omnigent.entities.scheduled_task import OwnerPreferences
from omnigent.evals.identity import is_eval_identity
from omnigent.memory.upkeep import MEMORY_POLICY
from omnigent.superchat.daily_notes.notes import local_date, local_zone, render_note
from omnigent.superchat.daily_notes.store import PASS_LEASE_SECONDS, SqlAlchemyDailyNoteStore
from omnigent.superchat.feature import topics_brief

_logger = logging.getLogger(__name__)

# The Helper Type the passes run as: it alone may write the note and memory (agents/notes/).
AGENT_TYPE = "notes"
# The work-queue kind every pass runs as.
JOB_KIND = "quiet_moment"
IDLE_SECONDS_ENV = "OMNIGENT_QUIET_MOMENT_IDLE_SECONDS"
# Minimum time between two passes for the same owner (0 = no gap beyond the idle rule).
MIN_GAP_SECONDS_ENV = "OMNIGENT_QUIET_MOMENT_MIN_GAP_SECONDS"
DEFAULT_IDLE_SECONDS = 1200.0
MIN_TURNS = 3
MAX_PASSES_PER_DAY = 4
# Helper runs one pass (finalize, Dreaming or People) of one day may use before it is left alone:
# a pass that keeps failing or writing nothing must not start a run at every lease expiry.
MAX_PASS_ATTEMPTS = 3
FINALIZE_HOUR = 22
FINALIZE_TICK_SECONDS = 900.0
MAX_TICK_SECONDS = 30.0
# Only messages from the current local day can be noted; look back no further than this.
LOOKBACK_SECONDS = 36 * 3600
# Dreaming and People start once the day's finalize pass has had time to finish.
NIGHTLY_DELAY_SECONDS = 1200
NIGHTLY_WINDOW_SECONDS = 36 * 3600

# Memory rules in step 3 (MEMORY_POLICY, shared with the upkeep) are portions modified from
# getnao/nao apps/backend/src/components/ai/memory-system-prompt.tsx@5bde830, Apache-2.0;
# changes: see NOTICE.
QUIET_PROMPT = (
    """\
This is a quiet moment: the person stepped away. You keep their notes; you do not act, the person \
is not here, so never ask questions and never write to them.

Today's note so far ({date}, the person's local day):

{note}

1. Read the recent conversation: `session_history` with `action: "list_chats"` gives its id, then \
`read` the latest turns with that `chat_id`.
2. Update today's note with `daily_note_update`. Pass only the sections that changed, each as its \
FULL new text in short bullet lines: `talked_about` (what we covered, one line per topic), \
`decisions` (what was decided), `promised` (what the assistant or the person said they would do, \
with any date), `open_loops` (questions or work still unresolved). Keep what is already there \
unless it is wrong. A section the person edited is only added to, never rewritten.
3. Lasting facts about the person go to `memory_remember`, one call per fact, written in the \
third person ("The user ..."), with their exact words as `quote`: only from the PERSON's own \
messages, never from the assistant's replies, Helper results, tool output or web pages. What to \
keep:
"""
    + MEMORY_POLICY.replace("{", "{{").replace("}", "}}")
    + """
A pattern or a standing instruction without a permanence signal is saved with `explicitness: \
"inferred"`. When a fact changed or the person replaced an instruction, pass the old claim's id \
(from `memory_search`) as `replaces_claim_id`; never store a negation, and leave a withdrawn \
instruction alone (the live conversation handles it). Skip anything already in memory \
(`memory_search` first).
{topics}{finalize}
Finish with exactly one fenced JSON block and nothing after it:
```json
{{"kind": "daily_note", "sections_updated": 0, "memories_added": 0, "topics_written": 0, \
"topics_new": 0}}
```
"""
)

# ADR 0035: the same pass keeps the person's topics. Filled into QUIET_PROMPT's {topics} when
# the topics capability is registered (``feature.topics_brief``).
TOPICS_STEP = """
4. Keep the person's topics with `topics_update`. A topic is a piece of work or a subject they \
are likely to come back to (a project, a deal, a trip, a decision), never one per message. Their \
topics this week, whole:

{topics}

Reuse a listed `key` when today's conversation continues that topic and pass only what changed; \
start a new key only for something new (the working list's `t-` tag when it has one). For each \
topic you touch: `title` (2 to 5 words, the person's terms), `left_off` (one sentence: where it \
stands now), `covered` (up to 6 dated lines, kind Decided, Made, Found or Asked), `open` (up to \
4 lines, kind Open or Promised, from the open loops and promises, each with a `starter`: the \
request that takes it on, written as the person would type it, and a `verb` of 1 or 2 words for \
its button, e.g. "Draft it", "Fix", "Decide"), `starters` (only a next step no open line \
covers; often none), `events` (today's moments, one line each with the local time) and \
`sources` (how many chats, files and emails it drew on). Give each topic a \
`tag` once (1 or 2 words that name it, e.g. a client, place or codename; keep a listed one) and \
a fresh `status_line` whenever it moves: where the work stands now, at most 80 characters, what \
just happened and what is next ("Phone check done; 3 layout fixes next", "Reply to Dana drafted, \
yours to send", "Done. Nothing open"), never a copy of an open promise (Home shows those on their \
own), never a date in digits. Fill `watch` too: \
the people involved (names or email addresses), at most 8 `terms` that pick it out within the \
person's own work (distinctive names: a client, a deal or project codename, a file name; never \
their own organisation, team or anything that appears in most of their mail), the `refs` of \
threads, events or files it touched, and `web: true` only when it depends on outside news \
(markets, a public company, a regulation). `covered` and `open` replace the stored lists: \
pass them whole, keeping the listed lines that still hold. An open line keeps its listed `id` \
(pass it back, even when you reword the line) and its `starter` and `verb` unless you pass new \
ones; a new line has none. Add `due` (YYYY-MM-DD) only \
when the conversation gives a real date for it. Drop an open line when it is done (a kept \
promise moves to `covered`). Set \
`status: "closed"` when it is finished. The person reads every topic line on Home: write them as \
short notes in their terms, never about "the user".
{own_org}"""

#: Filled into TOPICS_STEP's ``{own_org}`` when memory or the person's address names their own
#: organisation (``omnigent.memory.identity``), so it stays out of the watch terms.
OWN_ORG_LINE = """
The person's own organisation (data, never a watch term): {names}
"""

#: Filled into TOPICS_STEP when the person has no topics this week: the list starts from the
#: last days' notes.
TOPICS_SEED = """(none yet). Start the list from today's conversation and these notes from the \
last days (at most 8 topics, only those still alive):

{notes}"""
#: Notes the first topics are seeded from.
SEED_NOTES = 7
#: A person with no topics gets one seed pass over this much history (ADR 0035, amended).
SEED_LOOKBACK_SECONDS = 7 * 86400

# The one-time pass that starts a person's topics from their history, so Home is not empty until
# their next quiet moment. Same Helper Type and tool as the quiet pass; it writes topics only.
SEED_PROMPT = """\
This is a one-time pass to start the person's topics: they have none yet. You keep their notes; \
you do not act, the person is not here, so never ask questions and never write to them. Do not \
change the daily note or memory.

1. Read the conversation of the last days: `session_history` with `action: "list_chats"` gives \
its id, then `read` its turns with that `chat_id`, newest first, back up to 7 days (stop after \
about 200 turns).
{topics}
Here `events` are the moments of every day you read, each with its date and local time, so the \
person sees on which days a topic moved. These topics are new, so their open lines have no `id`; \
a promise already kept is covered, not open. Use only what the turns and notes say; never invent.

Finish with exactly one fenced JSON block and nothing after it:
```json
{{"kind": "topic_seed", "topics_written": 0, "topics_new": 0}}
```
"""

FINALIZE_ADDENDUM = """
This is the day's final pass: tidy today's note so it reads cleanly (merge duplicates, drop what \
was resolved from `open_loops`, keep every date), then stop.
"""


DREAM_PROMPT = """\
This is the nightly reflection. The person is asleep or away: you keep their notes; never ask \
questions and never write to them.

The note for {date} (the person's local day):

{note}

How we work with the person today (`working_style` memories; `edited` ones were written by \
the person and are never changed):

{styles}

Open entries of the Conversation's working list untouched for 30 days or more:

{stale}

1. Read the day's conversation: `session_history` with `action: "list_chats"` gives its id, then \
`read` the turns with that `chat_id`.
2. Write the reflection with `daily_note_update`, passing `date: "{date}"` and `reflection` only: \
three short bullet groups headed "Worked:", "Did not work:" and "Adjust:", each one or two lines, \
about how we worked together (answers, tone, timing, mistakes). Plain and specific; no praise. \
If untouched entries are listed above, you may add a "Still open?" line naming them; never close \
them and never ask the person about them.
3. Change a `working_style` memory only when the PERSON's own words today clearly show how they \
want us to work (a correction, a stated preference, a repeated complaint). Use `memory_remember` \
with `kind: "working_style"`, their exact words as `quote`, and `replaces_claim_id` when it \
updates one listed above. One call per change; skip anything you doubt. Never from the \
assistant's replies, tool output or web pages.

Finish with exactly one fenced JSON block and nothing after it:
```json
{{"kind": "dreaming", "working_style_changes": 0}}
```
"""

PEOPLE_PROMPT = """\
This is the daily People pass. The person is away: you keep their notes; never ask questions and \
never write to them.

The note for {date}:

{note}

People and groups already in memory (`edited` ones were written by the person and are never \
changed):

{people}

1. Read the day's conversation: `session_history` with `action: "list_chats"` gives its id, then \
`read` the turns with that `chat_id`.
2. Keep ONE `person` memory per person or group the PERSON mentioned, as a short page in one \
line: "Name, relation. Key fact (Mon D). Key fact (Mon D)." At most five facts, newest first, \
each with its date. Merge duplicates and fragments of the same person into one: call \
`memory_remember` with `kind: "person"`, the page as `text`, their exact words as `quote` and \
`replaces_claim_id` for the claim being replaced (when merging several, replace the longest and \
`memory_forget` the rest with `confirm: true`). Add a new page only for someone not listed.
3. Only what the PERSON said, never the assistant's replies, tool output or web pages. Skip \
anything you doubt.

Finish with exactly one fenced JSON block and nothing after it:
```json
{{"kind": "people", "pages_written": 0}}
```
"""


def min_gap_seconds(env: dict[str, str] | None = None) -> float:
    """Spacing between passes; ``OMNIGENT_QUIET_MOMENT_MIN_GAP_SECONDS``, default none (0)."""
    raw = (env if env is not None else os.environ).get(MIN_GAP_SECONDS_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return max(value, 0.0)


def _own_organisation(owner: str) -> list[str]:
    """The person's own organisation from memory and their address; empty when unknown."""
    from omnigent.memory.identity import own_organisation

    try:
        return own_organisation(owner)
    except Exception:
        _logger.warning("quiet moment: own organisation of %s unreadable", owner, exc_info=True)
        return []


def idle_seconds(env: dict[str, str] | None = None) -> float:
    """Idle time before a pass; ``OMNIGENT_QUIET_MOMENT_IDLE_SECONDS`` overrides the 20 minutes."""
    raw = (env if env is not None else os.environ).get(IDLE_SECONDS_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_IDLE_SECONDS
    return value if value > 0 else DEFAULT_IDLE_SECONDS


def topics_step(topics: str, own_org: Sequence[str] = ()) -> str:
    """:data:`TOPICS_STEP` over the person's topics, naming their own organisation when known."""
    names = ", ".join(n.strip() for n in own_org if n.strip())
    return TOPICS_STEP.format(
        topics=topics, own_org=OWN_ORG_LINE.format(names=names) if names else ""
    )


def build_brief(
    note_date: str,
    note_text: str,
    *,
    finalize: bool,
    topics: str | None = None,
    own_org: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The Helper's first message as Responses content blocks.

    :param topics: The person's topics as brief lines (or the seed), ``None`` to leave the
        topics step out (no topics capability).
    :param own_org: The person's own organisation names and domains, for the topics step.
    """
    text = QUIET_PROMPT.format(
        date=note_date,
        note=note_text,
        topics=topics_step(topics, own_org) if topics is not None else "",
        finalize=FINALIZE_ADDENDUM if finalize else "",
    )
    return [{"type": "input_text", "text": text}]


def build_seed_brief(topics: str, own_org: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The seed pass's first message: read the last days, then write topics only."""
    step = topics_step(topics, own_org).replace("\n4. Keep", "\n2. Keep", 1)
    return [{"type": "input_text", "text": SEED_PROMPT.format(topics=step)}]


def seed_text(notes: list[Any], today: str) -> str:
    """:data:`TOPICS_SEED` over the last :data:`SEED_NOTES` notes with content before *today*."""
    earlier = [n for n in notes if n.note_date < today and _has_content(n)][:SEED_NOTES]
    rendered = "\n\n".join(f"### {n.note_date}\n\n{render_note(n)}" for n in earlier)
    return TOPICS_SEED.format(notes=rendered or "(no earlier notes)")


def _in_quiet_hours(prefs: OwnerPreferences, now: float) -> bool:
    from omnigent.server.scheduled.helper_fire import _quiet_window_end

    return _quiet_window_end(prefs, now) is not None


def _claims_text(claims: list[dict[str, Any]]) -> str:
    """Claims as brief lines: id, whether the person wrote it, text."""
    if not claims:
        return "(none yet)"
    return "\n".join(
        f"- {c['claim_id']}{' [edited]' if c.get('person_authored') else ''}: {c['text']}"
        for c in claims
    )


def _has_content(note: Any) -> bool:
    return note is not None and any(
        note.sections.get(k, "").strip() for k in ("talked_about", "decisions", "promised")
    )


def _pass_wrote(note: Any, column: str) -> bool:
    """Whether a finished pass left what it exists to write.

    The finalize pass needs a note with content, Dreaming needs its reflection. The People pass
    writes memory pages only, so it counts as done when its Helper finished.
    """
    if column == "people_at":
        return True
    if note is None:
        return False
    if column == "dreamed_at":
        return bool(note.sections.get("reflection", "").strip())
    return _has_content(note)


class QuietMoment:
    """Ticks over persisted conversation data and runs the bounded pass for chats gone quiet."""

    def __init__(
        self,
        *,
        deps: Any,
        note_store: SqlAlchemyDailyNoteStore,
        idle_s: float | None = None,
    ) -> None:
        """
        :param deps: The ``FireDeps`` the ad-hoc Helper launch needs.
        :param note_store: Where the day's note and pass counter live.
        :param idle_s: Idle time before a pass (tests); defaults to :func:`idle_seconds`.
        """
        self._deps = deps
        self._notes = note_store
        self._idle_s = idle_s if idle_s is not None else idle_seconds()
        self._ticker: asyncio.Task[None] | None = None
        # Passes whose Helper may still run: ``(owner, day, column, helper id, started at)``.
        # Lost on a restart, which leaves the lease to expire and the pass to be retried.
        self._watching: list[tuple[str, str, str, str, float]] = []
        # Runs started per ``(owner, day, column)``, capped at :data:`MAX_PASS_ATTEMPTS`.
        self._attempts: dict[tuple[str, str, str], int] = {}
        self._last_slow_tick = 0.0
        # Owners whose topics are started or already exist; a restart forgets, and the next seed
        # check finds their topics (or none, and tries once more).
        self._seeded: set[str] = set()
        from omnigent.server import work_queue

        work_queue.register_handler(JOB_KIND, self._run_job)

    @property
    def tick_seconds(self) -> float:
        """How often idleness is re-derived from the stored messages."""
        return max(5.0, min(MAX_TICK_SECONDS, self._idle_s / 4))

    async def _gate(self, owner: str, now: float) -> tuple[OwnerPreferences, str] | None:
        """The owner's preferences and local day, or ``None`` when background work is held
        (always for an eval person: no notes, topics or passes, ADR 0042)."""
        if is_eval_identity(owner):
            return None
        prefs = await asyncio.to_thread(
            self._deps.scheduled_task_store.get_owner_preferences, owner
        )
        prefs = prefs or OwnerPreferences(user_id=owner)
        if prefs.proactivity == "off" or _in_quiet_hours(prefs, now):
            return None
        return prefs, local_date(prefs.timezone, datetime.fromtimestamp(now, local_zone("UTC")))

    async def idle_due(self, *, now: float | None = None) -> int:
        """Start a pass for each chat that went quiet; returns passes started.

        Idleness comes from stored data only: a chat is due when the person's last message is at
        least the idle time old and at least :data:`MIN_TURNS` of today's messages came after the
        day's last pass (the note's ``updated_at`` once it has a pass). A restart loses nothing.
        """
        instant = now if now is not None else time.time()
        recent = await asyncio.to_thread(
            self._notes.recent_user_messages, since=int(instant - LOOKBACK_SECONDS)
        )
        started = 0
        for session_id, stamps in recent.items():
            if instant - stamps[-1] < self._idle_s:
                continue
            try:
                if await self._run_pass(session_id, stamps, instant):
                    started += 1
            except Exception:
                _logger.exception("quiet moment: pass failed for %s", session_id)
        return started

    async def _run_pass(self, session_id: str, stamps: list[int], now: float) -> bool:
        """Run one quiet-moment pass for the chat if its gates allow; ``True`` when started."""
        owner = self._notes.owner_key(
            await asyncio.to_thread(self._deps.conversation_store.get_session_owner, session_id)
        )
        gate = await self._gate(owner, now)
        if gate is None:
            return False
        prefs, day = gate
        zone = local_zone(prefs.timezone)
        day_start = datetime.fromtimestamp(now, zone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        note = await asyncio.to_thread(self._notes.get, owner, day)
        baseline = day_start.timestamp()
        if note is not None and note.quiet_passes > 0:
            baseline = max(baseline, float(note.updated_at))
            # Space passes out: the note's updated_at is the last pass (or a later note edit).
            if now - float(note.updated_at) < min_gap_seconds():
                return False
        if sum(1 for stamp in stamps if stamp > baseline) < MIN_TURNS:
            return False
        claimed = await asyncio.to_thread(
            self._notes.claim_quiet_pass,
            owner,
            day,
            max_per_day=MAX_PASSES_PER_DAY,
            parent_session_id=session_id,
        )
        if not claimed:
            return False
        _logger.info("quiet moment: pass for %s (%s)", session_id, day)
        await self._start_helper(
            session_id, owner, day, finalize=False, key=f"{owner}:{day}:idle:{int(now)}"
        )
        return True

    async def _start_helper(
        self,
        session_id: str,
        owner: str,
        day: str,
        *,
        finalize: bool,
        key: str,
        watch: tuple[str, str, str, float] | None = None,
    ) -> str:
        """Queue the daily-note pass; returns its job id."""
        note = await asyncio.to_thread(self._notes.get, owner, day)
        content = build_brief(
            day,
            render_note(note),
            finalize=finalize,
            topics=await self._topics(owner, day),
            own_org=await asyncio.to_thread(_own_organisation, owner),
        )
        return await self._start_named(
            session_id, owner, "Daily note", content, key=key, watch=watch
        )

    async def _topics(self, owner: str, day: str) -> str | None:
        """The owner's topics for the brief; the seed from the last notes when they have none.

        ``None`` (no topics step) when no topics capability is registered.
        """
        listed = await asyncio.to_thread(topics_brief, owner)
        if listed is None or listed:
            return listed
        notes = await asyncio.to_thread(
            self._notes.list_range, owner, date_from=None, date_to=day, limit=SEED_NOTES + 1
        )
        return seed_text(notes, day)

    async def seed_due(self, *, now: float | None = None) -> int:
        """Start one seed pass for each person active this week who has no topics yet.

        Same gates as every pass (proactivity off or quiet hours hold it back); at most one per
        owner per process. ``0`` when no topics capability is registered.

        :returns: Helpers started.
        """
        instant = now if now is not None else time.time()
        recent = await asyncio.to_thread(
            self._notes.recent_user_messages, since=int(instant - SEED_LOOKBACK_SECONDS)
        )
        started = 0
        for session_id in recent:
            owner = self._notes.owner_key(
                await asyncio.to_thread(
                    self._deps.conversation_store.get_session_owner, session_id
                )
            )
            if owner in self._seeded:
                continue
            listed = await asyncio.to_thread(topics_brief, owner)
            if listed is None:
                return started
            if listed:
                self._seeded.add(owner)
                continue
            gate = await self._gate(owner, instant)
            if gate is None:
                continue
            _prefs, day = gate
            notes = await asyncio.to_thread(
                self._notes.list_range, owner, date_from=None, date_to=day, limit=SEED_NOTES + 1
            )
            self._seeded.add(owner)
            try:
                own_org = await asyncio.to_thread(_own_organisation, owner)
                await self._start_named(
                    session_id,
                    owner,
                    "Topics",
                    build_seed_brief(seed_text(notes, day), own_org),
                    key=f"{owner}:seed",
                )
                _logger.info("quiet moment: topic seed for %s", session_id)
                started += 1
            except Exception:
                _logger.exception("quiet moment: topic seed failed for %s", session_id)
        return started

    async def finalize_due(self, *, now: float | None = None) -> int:
        """Finalize notes whose day is ending (local evening) or ended; returns passes started."""
        instant = now if now is not None else time.time()
        started = 0
        for note in await asyncio.to_thread(self._notes.pending_finalize):
            if note.parent_session_id is None:
                continue
            prefs = await asyncio.to_thread(
                self._deps.scheduled_task_store.get_owner_preferences, note.owner
            )
            prefs = prefs or OwnerPreferences(user_id=note.owner)
            zone = local_zone(prefs.timezone)
            local = datetime.fromtimestamp(instant, zone)
            ended = note.note_date < local.date().isoformat()
            if not (ended or local.hour >= FINALIZE_HOUR):
                continue
            if prefs.proactivity == "off" or _in_quiet_hours(prefs, instant):
                continue
            key = (note.owner, note.note_date, "finalized_at")
            if not ended and self._attempts.get(key, 0) >= MAX_PASS_ATTEMPTS:
                continue
            if not await asyncio.to_thread(self._notes.claim_finalize, note.owner, note.note_date):
                continue
            if ended:
                # The helper writes "today's" note, so a day that already ended is only closed.
                await asyncio.to_thread(
                    self._notes.complete_pass, note.owner, note.note_date, "finalized_at"
                )
                continue
            try:
                await self._start_helper(
                    note.parent_session_id,
                    note.owner,
                    note.note_date,
                    finalize=True,
                    key=":".join(key),
                    watch=(*key, instant),
                )
                self._attempts[key] = self._attempts.get(key, 0) + 1
                started += 1
            except Exception:
                _logger.exception("quiet moment: finalize failed for %s", note.owner)
        return started

    async def nightly_due(self, *, now: float | None = None) -> int:
        """Start the Dreaming and People passes for days finalized a while ago.

        Same gates as the finalize pass (proactivity off or quiet hours hold them back), each
        pass at most once per day, ``notes`` Helpers that never message the person.

        :returns: Helpers started.
        """
        from omnigent.runtime import get_memory_service

        instant = int(now if now is not None else time.time())
        service = get_memory_service()
        started = 0
        for note in await asyncio.to_thread(
            self._notes.pending_nightly,
            finalized_after=instant - NIGHTLY_WINDOW_SECONDS,
            finalized_before=instant - NIGHTLY_DELAY_SECONDS,
        ):
            if note.parent_session_id is None:
                continue
            prefs = await asyncio.to_thread(
                self._deps.scheduled_task_store.get_owner_preferences, note.owner
            )
            prefs = prefs or OwnerPreferences(user_id=note.owner)
            if prefs.proactivity == "off" or _in_quiet_hours(prefs, instant):
                continue
            # One lease per note: Dreaming, then People once Dreaming is done.
            for column, name, template, kinds in (
                ("dreamed_at", "Dreaming", DREAM_PROMPT, ["working_style"]),
                ("people_at", "People", PEOPLE_PROMPT, ["person"]),
            ):
                key = (note.owner, note.note_date, column)
                if getattr(note, column) is not None:
                    continue
                if self._attempts.get(key, 0) >= MAX_PASS_ATTEMPTS:
                    continue  # given up on this pass; the next one may still run
                if service is None or not await asyncio.to_thread(
                    self._notes.claim_pass, note.owner, note.note_date, column
                ):
                    break
                try:
                    claims = await asyncio.to_thread(service.list_claims, note.owner, kinds=kinds)
                    text = template.format(
                        date=note.note_date,
                        note=render_note(note),
                        styles=_claims_text(claims),
                        people=_claims_text(claims),
                        stale=await asyncio.to_thread(self._stale_entries, note.parent_session_id),
                    )
                    await self._start_named(
                        note.parent_session_id,
                        note.owner,
                        name,
                        [{"type": "input_text", "text": text}],
                        key=":".join(key),
                        watch=(*key, float(instant)),
                    )
                    self._attempts[key] = self._attempts.get(key, 0) + 1
                    started += 1
                except Exception:
                    _logger.exception("quiet moment: %s pass failed for %s", name, note.owner)
                break
        return started

    def _stale_entries(self, chat_id: str) -> str:
        """The entries marked ``stale?`` in the chat's latest checkpoint, one per line."""
        try:
            page = self._deps.conversation_store.list_items(
                chat_id, limit=1, order="desc", type="compaction"
            )
            summary = getattr(page.data[0].data, "summary", None) if page.data else None
        except Exception:  # noqa: BLE001 - the reflection runs without them
            _logger.warning("quiet moment: could not read the checkpoint of %s", chat_id)
            summary = None
        mark = f"{STALE_MARK}]"
        lines = [line.strip() for line in (summary or "").splitlines() if mark in line]
        return "\n".join(lines) or "(none)"

    async def _start_named(
        self,
        session_id: str,
        owner: str,
        name: str,
        content: list[dict[str, Any]],
        *,
        key: str,
        watch: tuple[str, str, str, float] | None = None,
    ) -> str:
        """Queue a pass as a work-queue job; it starts its Helper when the owner has room.

        :param key: The pass's idempotency key: a pass already waiting or running is not queued
            twice.
        :param watch: ``(owner, day, column, started)`` when the pass sets a done marker: its
            Helper is watched by :meth:`settle` once it started.
        :returns: The job id.
        """
        from omnigent.server import work_queue

        payload = {
            "session_id": session_id,
            "owner": owner,
            "name": name,
            "content": content,
            "watch": list(watch) if watch is not None else None,
        }
        return await asyncio.to_thread(
            work_queue.submit, owner, JOB_KIND, key, payload, conversation_id=session_id
        )

    async def _run_job(self, job: Any) -> str:
        """The ``quiet_moment`` job: start the pass's ad-hoc Helper; the job holds the owner's
        slot until the Helper's turn ends."""
        from omnigent.server.scheduled.helper_fire import start_adhoc_helper

        payload = job.payload
        owner = payload["owner"]
        helper_id = await start_adhoc_helper(
            self._deps,
            parent_session_id=payload["session_id"],
            agent_type=AGENT_TYPE,
            name=payload["name"],
            user_id=None if owner == self._notes.owner_key(None) else owner,
            content=payload["content"],
        )
        watch = payload.get("watch")
        if watch:
            w_owner, day, column, started = watch
            self._watching.append((w_owner, day, column, helper_id, float(started)))
        return helper_id

    async def settle(self, *, now: float | None = None) -> int:
        """Set the done marker of each started pass whose Helper finished and wrote content.

        A Helper that failed, wrote nothing or outlived the lease is dropped: its lease stays
        and the pass is retried once the lease expires.

        :returns: Markers set.
        """
        instant = now if now is not None else time.time()
        done = 0
        for watch in list(self._watching):
            owner, day, column, helper_id, started = watch
            helper = await asyncio.to_thread(
                self._deps.conversation_store.get_conversation, helper_id
            )
            status = helper.live_status if helper is not None else "failed"
            if status in (None, "running", "waiting") and instant - started < PASS_LEASE_SECONDS:
                continue
            self._watching.remove(watch)
            if status != "idle":
                continue
            note = await asyncio.to_thread(self._notes.get, owner, day)
            if _pass_wrote(note, column):
                await asyncio.to_thread(self._notes.complete_pass, owner, day, column)
                self._attempts.pop((owner, day, column), None)
                done += 1
        return done

    async def start(self) -> None:
        """Start the ticker."""
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick(), name="daily-note-ticker")

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self.tick_seconds)
            try:
                await self.settle()
                await self.idle_due()
                if time.monotonic() - self._last_slow_tick >= FINALIZE_TICK_SECONDS:
                    self._last_slow_tick = time.monotonic()
                    await self.seed_due()
                    await self.finalize_due()
                    await self.nightly_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("quiet moment: tick failed")

    async def shutdown(self) -> None:
        """Cancel the ticker."""
        if self._ticker is not None:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
        self._ticker = None
