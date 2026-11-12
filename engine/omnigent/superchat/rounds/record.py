"""Write a Round's events where the engine sees each fact (ADR 0029), and journal it once settled.

* :func:`assign_round` is the one rule for which Round a stored message belongs to.
* :class:`Recorder` appends events in order on a background task: a write never blocks the
  turn or the request that saw the fact, and a failed write is logged, never raised. An event
  recorded from a worker thread is handed to the loop the recorder last ran on. Streamed
  words are joined per :data:`WORDS_EVERY` seconds. Each stored batch is handed to the
  recorder's ``on_written`` (:mod:`omnigent.superchat.rounds.read`).
* :class:`RelayRound` turns one session's runner stream, as the server relays it, into events:
  turns, words, tool calls, a Helper's finish, chips and checklist changes.
* :func:`note_message`, :func:`helper_created`, :func:`helper_titled` and :func:`file_saved`
  are the writers at a stored message, a Helper's creation, its model-written name and an
  artifact save.
* :func:`stop_round` is Stop: it picks the Round the person stops, records ``stop.requested``
  and returns the Stop line; :func:`stopped_wake` tells a Helper's late notice of a stopped
  Round from one that may still start a turn.
* Settlement is detected where the settling events are written: after the :class:`Recorder`
  stored a ``turn.ended``, ``helper.finished`` or ``stop.requested``, it folds that Round and,
  the first time the fold says settled, hands :func:`journal_entry` of the view to its
  ``on_settled`` sink (the Round's one journal entry, written in the host).
* :func:`close_lost_turns` ends, from the stored conversation, a turn whose stream the relay
  lost (its runner tunnel dropped mid-turn): the relay calls it when its session reports idle
  or its relay ends for good, and :class:`Sweep` every :data:`SWEEP_EVERY` seconds for a turn
  open longer than :data:`LOST_AFTER` whose session is idle. The sweep also settles a Round no
  turn is coming for (:func:`settle_stale`), and a reset settles every Round of its
  Conversation (:func:`reset_rounds`). A Round cannot stay running past its sessions going idle.
* :func:`turn_failed` ends a Round whose turn failed before reaching a runner; when the
  Conversation's session goes idle, a Round whose Helper results were steered into a turn
  records ``session.idle`` and settles.

Only Superchat sessions are recorded; a session outside a Round records nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from omnigent.context.labels import (
    BACKGROUND_LABELS,
    ROUND_LABEL_KEY,
    is_artifact_chat,
    is_superside_chat,
)
from omnigent.context.tool_digest import call_digest
from omnigent.entities.session_project import project_slug_from_workspace
from omnigent.harnesses.executor import CHECKLIST_TOOL
from omnigent.harnesses.tracing import exports_spans
from omnigent.runner.tool_starts import STOPPED_OUTPUT
from omnigent.superchat.artifact_kinds import is_deck_name
from omnigent.superchat.cards.tools import CARD_TOOL_NAME, TERMINAL_CARDS
from omnigent.superchat.projects.card import workspace_root
from omnigent.superchat.rounds import trace as round_trace
from omnigent.superchat.rounds.checklist import STATUSES
from omnigent.superchat.rounds.fold import (
    ASSISTANT,
    CHECKLIST_CREATED,
    CHECKLIST_UPDATED,
    CHIPS,
    FILE_SAVED,
    HELPER,
    HELPER_FINISHED,
    HELPER_STARTED,
    HELPER_TITLED,
    PERSON,
    ROUND_OPENED,
    SESSION_IDLE,
    STOP_REQUESTED,
    SYSTEM,
    TOOL_CALLED,
    TOOL_INPUT,
    TOOL_RESULT,
    TOOL_STARTED,
    TURN_ENDED,
    TURN_STARTED,
    WAKE,
    WORDS,
    fold,
)
from omnigent.superchat.rounds.store import RoundEvent, SqlAlchemyRoundStore
from omnigent.superchat.subagents import helper_folder
from omnigent.superchat.tasks.record import CHECK_TOOLS
from omnigent.superchat.titles import clip, one_line_summary

_logger = logging.getLogger(__name__)

#: Seconds of streamed words joined into one ``words`` event.
WORDS_EVERY = 0.25
#: Seconds between two ``tool.input`` events of one call.
INPUT_EVERY = 1.0
#: Longest Helper result kept on ``helper.finished``.
RESULT_LIMIT = 500
#: The stream event a harness sends as a tool call's arguments stream (see the executor adapter).
TOOL_INPUT_DELTA = "response.function_call_arguments.delta"
#: A turn's terminal stream events and the status each gives a Helper that ends with it.
_TERMINAL = {
    "response.completed": "done",
    "response.failed": "failed",
    "response.incomplete": "failed",
    "response.cancelled": "stopped",
}
#: Native checklist tools (Claude's task tools); the ``checklist`` tool writes the same events.
_CHECKLIST_CREATE, _CHECKLIST_UPDATE = "TaskCreate", "TaskUpdate"
_TASK_NUMBER = re.compile(r"#(\d+)")
#: The tool whose arguments are the follow-up chips.
_CHIPS_TOOL = "suggest_follow_ups"
#: Events after which a Round may have settled; the fold decides.
_SETTLING = frozenset({TURN_ENDED, HELPER_FINISHED, STOP_REQUESTED})
#: Longest ask kept in a journal entry.
ASK_LIMIT = 120
#: Settled Rounds remembered, so a late event does not fold a settled Round again.
_SETTLED_KEPT = 4096
#: An agent name a Helper's stored title may open with (``worker`` in ``worker:Q3 deck``).
_AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]*")

#: ``(conversation id, journal entry) -> None``: where a settled Round's entry goes.
Settled = Callable[[str, dict[str, Any]], Awaitable[None]]
#: ``session id -> whether it runs no turn``: what :func:`close_lost_turns` asks of a session.
Idle = Callable[[str], Awaitable[bool]]
#: Seconds between two passes of the :class:`Sweep`.
SWEEP_EVERY = 30.0
#: Seconds a turn stays open before the :class:`Sweep` reads it as lost when its session is idle.
LOST_AFTER = 60.0
#: Seconds back the :class:`Sweep` looks, when it starts, for Rounds left with an open turn.
SWEEP_LOOKBACK = 86400.0
#: The reason on the ``turn.ended`` of a lost turn whose session stored no reply.
RELAY_LOST = "relay lost"
#: Newest messages of a session read for a lost turn's reply.
_REPLIES = 50
#: Seconds an unsettled Round goes without an event, its Conversation idle and no Helper of it
#: running, before the :class:`Sweep` reads it as one no turn is coming for.
STALE_AFTER = 600.0
#: The reasons on the ``turn.ended`` that settles a Round no turn will run for: its
#: Conversation was reset, or nothing happened in it for :data:`STALE_AFTER` seconds.
RESET, NO_TURN = "reset", "no turn"
#: Newest Rounds of a Conversation a reset reads for those still unsettled.
_RESET_ROUNDS = 20
#: Newest Rounds of a Conversation read, as its session goes idle, for steered results.
_IDLE_ROUNDS = 5
#: The reason on the ``turn.ended`` that ends a Round whose own ``turn.ended`` was not stored.
EVENTS_LOST = "round events not stored"
#: Seconds shutdown waits for queued events and journal entries.
JOURNAL_DRAIN_S = 10.0
#: Seconds before the one retry of a batch the store did not take.
_RETRY_AFTER = 1.0


@dataclass(frozen=True)
class Assignment:
    """The Round a stored message belongs to.

    :param round_id: The Round.
    :param opened: The message opens it (a ``round.opened`` follows).
    :param actor: Who opened it: ``person``, or ``system`` for a hidden Round.
    """

    round_id: str
    opened: bool = False
    actor: str = PERSON


def assign_round(
    *,
    item_id: str,
    labels: Mapping[str, str] | None,
    helper: bool,
    by_person: bool,
    turn_running: bool,
    helper_round: str | None = None,
    current_settled: bool = False,
) -> Assignment | None:
    """The Round a message joins, decided once, when the message is stored.

    The rule is ADR 0029's: "A Round is one message from the person through its settlement. A
    steer joins the running turn's Round. A wake, notice or timer turn carries the Round of the
    Helper that caused it. Scheduled and system runs get a hidden Round of their own." Here:

    1. Outside a Superchat session there is no Round.
    2. A run no person started (its session carries a background label) and that has no Round
       yet opens a hidden one: actor ``system``, id the message's own.
    3. A Helper's messages (its brief, a send to it) join the Round it was created in.
    4. A notice that is not the person's words (a Helper's wake or progress, a timer) joins the
       Round of the Helper that sent it, else the session's current Round.
    5. The person's message while a turn runs is a steer: it joins the current Round, unless
       that Round has settled (a Stop settles it at once, before its turn winds down).
    6. Any other message from the person opens a new Round whose id is the message's id.

    :param item_id: The stored message's id.
    :param labels: The session's labels; :data:`ROUND_LABEL_KEY` is its current Round.
    :param helper: The session is a Helper.
    :param by_person: The message is the person's words, not a notice.
    :param turn_running: A turn of the session runs as the message is stored.
    :param helper_round: The Round of the Helper a notice came from, when it names one.
    :param current_settled: The current Round has settled; nothing can steer it.
    :returns: The Round, or ``None`` when the message is in none.
    """
    if not is_superside_chat(labels):
        return None
    current = (labels or {}).get(ROUND_LABEL_KEY) or None
    if current is None and any(key in (labels or {}) for key in BACKGROUND_LABELS):
        return Assignment(item_id, opened=True, actor=SYSTEM)
    if helper:
        return Assignment(current) if current else None
    if not by_person:
        joined = helper_round or current
        return Assignment(joined) if joined else None
    if turn_running and current and not current_settled:
        return Assignment(current)
    return Assignment(item_id, opened=True)


def checked(view: Mapping[str, Any]) -> bool | None:
    """Whether the Round's deck was checked: a ``deck_check`` in its trail, the Assistant's or a
    Helper's, came back clean; ``None`` for a Round that saved no deck and ran no check."""
    check = CHECK_TOOLS["deck"]
    checks = [t for t in view["trail"] if str(t.get("name") or "").rsplit("__", 1)[-1] == check]
    if not checks and not any(is_deck_name(str(f.get("name") or "")) for f in view["files"]):
        return None
    return any(t.get("ok") for t in checks)


def journal_entry(round_id: str, view: Mapping[str, Any]) -> dict[str, Any] | None:
    """The journal entry of a settled Round, built only from its view (:func:`fold`).

    :returns: ``round_id``, ``date`` (the settlement's, UTC), ``ask`` (the first line of the
        person's message, clipped), ``outcome`` (the first sentence of the last Assistant
        text), ``stopped``, ``files`` (names), ``helpers`` (titles), ``folder`` (the Helpers'
        folders), ``seconds``, ``checked`` (:func:`checked`) and ``refs``; ``None`` for a Round
        not settled or hidden (a run no person started writes no entry).
    """
    if view.get("settled_at") is None or view.get("hidden"):
        return None
    asked = [line.strip() for line in str(view.get("ask") or "").splitlines() if line.strip()]
    said = [w for w in view.get("words") or [] if w.strip()]
    helpers = list(view["helpers"].values())
    folders = list(dict.fromkeys(h["folder"] for h in helpers if h.get("folder")))
    files = [f for f in view["files"] if f.get("name")]
    return {
        "round_id": round_id,
        "date": f"{datetime.fromtimestamp(view['settled_at'], UTC):%Y-%m-%d}",
        "ask": clip(asked[0], ASK_LIMIT) if asked else "",
        "outcome": (one_line_summary(said[-1]) or "") if said else "",
        "stopped": view["status"] == "stopped",
        "files": [f["name"] for f in files],
        "helpers": [h["title"] for h in helpers if h.get("title")],
        "folder": ", ".join(folders),
        "seconds": view["elapsed"],
        "checked": checked(view),
        "refs": {
            "artifacts": [
                f"{f['artifact_id']}@v{f['version']}"
                for f in files
                if f.get("artifact_id") and f.get("version")
            ],
            # A folder is relative to the workspace (``projects/q3``).
            "project": list(
                dict.fromkeys(
                    p for f in folders if (p := project_slug_from_workspace(f"/workspace/{f}"))
                )
            ),
            "helpers": [h["id"] for h in helpers],
        },
    }


class Recorder:
    """Appends Round events in order on one background task; never blocks, never raises."""

    def __init__(
        self,
        store: SqlAlchemyRoundStore,
        on_written: Callable[[Sequence[RoundEvent]], None] | None = None,
        on_settled: Settled | None = None,
    ) -> None:
        """
        :param on_written: Called on the loop with each batch once it is stored.
        :param on_settled: Given each settled Round's :func:`journal_entry`, once per Round in
            this process; the writer it reaches keeps it once across processes too.
        """
        self._store = store
        self._on_written = on_written
        self._queue: list[RoundEvent] = []
        self._drain: asyncio.Task[None] | None = None
        self._on_settled = on_settled
        self._settled: dict[str, None] = {}
        self._journals: set[asyncio.Task[None]] = set()
        #: ``(round, actor, helper) -> [first chunk ms, conversation, chunks]`` not yet queued.
        self._words: dict[tuple[str, str, str | None], list[Any]] = {}
        #: ``round -> conversation`` of each Round opened or a turn started in that the
        #: :class:`Sweep` has not yet seen settled with no open turn (at start, it adds every
        #: Round the store holds from the last :data:`SWEEP_LOOKBACK` seconds).
        self.turned: dict[str, str] = {}
        #: Held by :func:`close_lost_turns`, so two never end one turn.
        self.closing = asyncio.Lock()
        #: The loop events were last recorded on: a worker thread's event is handed to it.
        self._loop: asyncio.AbstractEventLoop | None = None

    def record(
        self, round_id: str, conversation_id: str, kind: str, actor: str, **body: Any
    ) -> None:
        """Queue one event of *round_id*, after any words of that Round still being joined."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # a worker thread: the loop records it
            if self._loop is None or self._loop.is_closed():
                _logger.warning("round event %s dropped: no loop", kind)
                return
            self._loop.call_soon_threadsafe(
                lambda: self.record(round_id, conversation_id, kind, actor, **body)
            )
            return
        self._flush_words(round_id)
        self._queue_event(
            RoundEvent(round_id, conversation_id, kind, actor, _now_ms(), _clean(body))
        )

    def words(
        self,
        round_id: str,
        conversation_id: str,
        actor: str,
        text: str,
        helper_id: str | None = None,
    ) -> None:
        """Join a streamed chunk into the actor's ``words`` event, queued every
        :data:`WORDS_EVERY` seconds or before the Round's next event."""
        key = (round_id, actor, helper_id)
        pending = self._words.setdefault(key, [_now_ms(), conversation_id, []])
        pending[2].append(text)
        if _now_ms() - pending[0] >= WORDS_EVERY * 1000:
            self._flush_words(round_id)

    def _flush_words(self, round_id: str) -> None:
        for key in [k for k in self._words if k[0] == round_id]:
            at_ms, conversation_id, chunks = self._words.pop(key)
            body = _clean({"text": "".join(chunks), "helper_id": key[2]})
            self._queue_event(RoundEvent(round_id, conversation_id, WORDS, key[1], at_ms, body))

    def _queue_event(self, event: RoundEvent) -> None:
        log = _logger.debug if event.kind in (WORDS, TOOL_INPUT) else _logger.info
        log("round %s %s %s %s", event.round_id, event.kind, event.actor, event.body)
        if event.kind in (ROUND_OPENED, TURN_STARTED):
            self.turned[event.round_id] = event.conversation_id
        self._queue.append(event)
        if self._drain is None or self._drain.done():
            try:
                self._drain = asyncio.get_running_loop().create_task(self._write())
            except RuntimeError:  # no loop: nothing in the engine records from a bare thread
                _logger.warning("round events dropped: no running loop")
                self._queue.clear()

    async def _write(self) -> None:
        while self._queue:
            batch, self._queue = self._queue, []
            try:
                await asyncio.to_thread(self._store.append, batch)
            except Exception:
                _logger.warning("round events not stored, retrying once", exc_info=True)
                await asyncio.sleep(_RETRY_AFTER)
                try:
                    await asyncio.to_thread(self._store.append, batch)
                except Exception:  # noqa: BLE001 — a second failure drops the batch, logged there
                    self._dropped(batch)
                    continue
            if self._on_written is not None:
                try:
                    self._on_written(batch)
                except Exception:
                    _logger.warning("round events stored, not announced", exc_info=True)
            await self._settle(batch)

    def _dropped(self, batch: Sequence[RoundEvent]) -> None:
        """A batch the store refused twice is lost. A Round whose Assistant ``turn.ended`` was
        in it ends failed, reason :data:`EVENTS_LOST`, once the store takes events again, so it
        never reads "working" over a lost end; the Sweep ends other turns it lost from the
        stored conversation."""
        _logger.error("round events not stored (%d dropped)", len(batch))
        lost = {
            e.round_id: e.conversation_id
            for e in batch
            if e.kind == TURN_ENDED
            and e.actor == ASSISTANT
            and e.body.get("reason") != EVENTS_LOST
        }
        for round_id, conversation_id in lost.items():
            self._queue_event(
                RoundEvent(
                    round_id,
                    conversation_id,
                    TURN_ENDED,
                    ASSISTANT,
                    _now_ms(),
                    {"status": "failed", "reason": EVENTS_LOST},
                )
            )

    async def events(self, round_id: str) -> list[dict[str, Any]]:
        """The Round's stored events, once every queued one is written."""
        await self.idle()
        return await asyncio.to_thread(self._store.events, round_id)

    async def recent(self, conversation_id: str, limit: int) -> list[str]:
        """The Conversation's *limit* newest Rounds, once every queued event is written."""
        await self.idle()
        return await asyncio.to_thread(self._store.recent, conversation_id, limit)

    async def view(self, round_id: str) -> dict[str, Any]:
        """The Round's view, folded from its stored events once every queued one is written."""
        return fold(await self.events(round_id), round_id)

    async def _settle(self, batch: list[RoundEvent]) -> None:
        """Fold each Round a settling event of *batch* is in; journal the newly settled ones."""
        if self._on_settled is None:
            return
        maybe = {
            e.round_id: e.conversation_id
            for e in batch
            if e.kind in _SETTLING and e.round_id not in self._settled
        }
        for round_id, conversation_id in maybe.items():
            try:
                view = fold(await asyncio.to_thread(self._store.events, round_id), round_id)
            except Exception:
                _logger.warning("round %s not folded", round_id, exc_info=True)
                continue
            if view["settled_at"] is None:
                continue
            self._settled[round_id] = None
            round_trace.end_span(round_id, view)
            while len(self._settled) > _SETTLED_KEPT:
                del self._settled[next(iter(self._settled))]
            entry = journal_entry(round_id, view)
            if entry is not None:
                task = asyncio.get_running_loop().create_task(
                    _journal(self._on_settled, conversation_id, entry)
                )
                self._journals.add(task)
                task.add_done_callback(self._journals.discard)

    async def idle(self) -> None:
        """Wait until every queued event is written. Never waits for a journal entry: Stop, a
        steer and a Round's view read through here."""
        for round_id in {k[0] for k in self._words}:
            self._flush_words(round_id)
        while self._drain is not None and not self._drain.done():
            await asyncio.shield(self._drain)

    async def drain(self) -> None:
        """Wait until every queued event is written and every entry journaled (shutdown)."""
        await self.idle()
        if self._journals:
            await asyncio.gather(*self._journals)


async def _journal(sink: Settled, conversation_id: str, entry: dict[str, Any]) -> None:
    try:
        await sink(conversation_id, entry)
    except Exception:
        _logger.warning("round %s not journaled", entry["round_id"], exc_info=True)


#: The engine's recorder, set when the rounds feature is installed; ``None`` records nothing.
RECORDER: Recorder | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clean(body: Mapping[str, Any]) -> dict[str, Any]:
    """*body* without its empty fields."""
    return {k: v for k, v in body.items() if v is not None}


def _record(round_id: str, conversation_id: str, kind: str, actor: str, **body: Any) -> None:
    if RECORDER is not None:
        RECORDER.record(round_id, conversation_id, kind, actor, **body)


def _is_helper(conversation: Any) -> bool:
    """Whether a session acts as a Helper in its Round. A run no person started is the main
    actor of its own hidden Round, even when it is a child session."""
    labels = conversation.labels or {}
    return conversation.kind == "sub_agent" and not any(k in labels for k in BACKGROUND_LABELS)


def round_of(labels: Mapping[str, str] | None) -> str | None:
    """The Round a session's labels say it is in, for a Superchat session."""
    if not is_superside_chat(labels):
        return None
    return (labels or {}).get(ROUND_LABEL_KEY) or None


def _text(data: Mapping[str, Any]) -> str:
    parts = data.get("content")
    return (
        "".join(str(p.get("text") or "") for p in parts if isinstance(p, Mapping))
        if isinstance(parts, list)
        else ""
    )


def _by_person(data: Mapping[str, Any]) -> bool:
    return data.get("role", "user") == "user" and data.get("is_system_notice") is not True


async def _notice_helper(
    conversation_store: Any, conversation: Any, context: Mapping[str, Any] | None
) -> tuple[str | None, str | None]:
    """The Helper of *conversation* a notice names in its ``context``, and that Helper's Round."""
    named = (context or {}).get("helper_id")
    if not isinstance(named, str) or not named:
        return None, None
    helper = await asyncio.to_thread(conversation_store.get_conversation, named)
    if helper is None or helper.parent_conversation_id != conversation.id:
        return None, None
    return named, round_of(helper.labels)


async def note_message(
    conversation_store: Any,
    conversation: Any,
    item_id: str,
    data: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    *,
    turn_running: bool,
) -> None:
    """Give a stored message its Round (:func:`assign_round`) and record what it starts.

    A person's message that opens a Round records ``round.opened``; a Helper's notice records
    ``wake`` (``steered`` when a turn runs to take it). When the Round is not the session's
    current one, the session's :data:`ROUND_LABEL_KEY` moves to it before the turn starts. A
    notice never moves it while a turn runs (the running turn's Round keeps its steers, Stop and
    files) nor off a Round still waiting for its first turn: that turn is the person's
    message's, though the notice came in while a cold host started. Never raises.

    :param conversation: The session's row.
    :param item_id: The stored message's id.
    :param data: The message's data (``role``, ``content``, ``is_system_notice``).
    :param context: The message's ``context``; a Helper's notice names ``helper_id``.
    :param turn_running: A turn of the session runs as it is stored.
    """
    try:
        labels = conversation.labels
        if not is_superside_chat(labels):
            return
        by_person = _by_person(data)
        helper_id, helper_round = (
            (None, None)
            if by_person
            else await _notice_helper(conversation_store, conversation, context)
        )
        current = labels.get(ROUND_LABEL_KEY)
        assigned = assign_round(
            item_id=item_id,
            labels=labels,
            helper=conversation.kind == "sub_agent",
            by_person=by_person,
            turn_running=turn_running,
            helper_round=helper_round,
            current_settled=bool(
                by_person
                and turn_running
                and current
                and RECORDER is not None
                and (await RECORDER.view(current))["settled_at"] is not None
            ),
        )
        if assigned is None:
            return
        waiting = _WAITING.get(conversation.id)
        if (
            assigned.round_id != current
            and (by_person or not turn_running)
            and waiting in (None, assigned.round_id)
        ):
            await asyncio.to_thread(
                conversation_store.set_labels,
                conversation.id,
                {ROUND_LABEL_KEY: assigned.round_id},
            )
        root = conversation.root_conversation_id or conversation.id
        traced: dict[str, str] = {}
        if exports_spans():
            if assigned.opened:
                traced = round_trace.open_span(assigned.round_id, root, labels, _text(data))
            parent = round_trace.traceparent(assigned.round_id)
            if parent is None and not assigned.opened and RECORDER is not None:
                parent = round_trace.traceparent(
                    assigned.round_id, await RECORDER.events(assigned.round_id)
                )
            round_trace.note(conversation.id, parent)
        if assigned.opened:
            _WAITING[conversation.id] = assigned.round_id
            text = _text(data)
            _record(
                assigned.round_id,
                root,
                ROUND_OPENED,
                assigned.actor,
                text=text,
                item_id=item_id,
                **traced,
            )
        elif helper_id is not None:
            _record(
                assigned.round_id,
                root,
                WAKE,
                SYSTEM,
                helper_id=helper_id,
                item_id=item_id,
                steered=True if turn_running else None,
            )
    except Exception:
        _logger.warning("message %s not given a Round", item_id, exc_info=True)


def helper_labels(
    parent_labels: Mapping[str, str] | None, child_labels: Mapping[str, str]
) -> dict[str, str]:
    """The Round label a child session gets at creation: a Helper belongs to the Round its
    parent is in. A file's studio and a run no person started (a scheduled Helper) get none.
    """
    round_id = round_of(parent_labels)
    if round_id is None or is_artifact_chat(child_labels):
        return {}
    if any(key in child_labels for key in BACKGROUND_LABELS):
        return {}
    return {ROUND_LABEL_KEY: round_id}


def helper_created(conversation: Any) -> None:
    """Record ``helper.started`` for a Helper session just created in a Round."""
    round_id = round_of(conversation.labels)
    if round_id is None or conversation.parent_conversation_id is None:
        return
    agent, title = _agent_title(conversation)
    _record(
        round_id,
        conversation.root_conversation_id or conversation.id,
        HELPER_STARTED,
        ASSISTANT,
        helper_id=conversation.id,
        title=title,
        agent=agent,
        folder=helper_folder(conversation.workspace, workspace_root()),
    )


def helper_titled(conversation: Any) -> None:
    """Record ``helper.titled`` for a Helper in a Round its background titler just named
    (its ``task_summary``), so the strip and Activity show the one name."""
    round_id = round_of(conversation.labels)
    title = getattr(conversation, "task_summary", None)
    if round_id is None or conversation.parent_conversation_id is None or not title:
        return
    _record(
        round_id,
        conversation.root_conversation_id or conversation.id,
        HELPER_TITLED,
        ASSISTANT,
        helper_id=conversation.id,
        title=title,
    )


def _agent_title(conversation: Any) -> tuple[str | None, str | None]:
    """A Helper's agent and the title its model gave, from a stored ``<agent>:<title>``."""
    title = conversation.title or ""
    agent = getattr(conversation, "sub_agent_name", None)
    head, colon, rest = title.partition(":")
    if colon and (head == agent or (agent is None and _AGENT_NAME.fullmatch(head))):
        return head, rest.strip() or None
    return agent, title or None


def file_saved(saver: Any, artifact: Any) -> None:
    """Record ``file.saved`` for an artifact version saved by a session in a Round."""
    round_id = round_of(saver.labels)
    if round_id is None:
        return
    helper = _is_helper(saver)
    _record(
        round_id,
        saver.root_conversation_id or saver.id,
        FILE_SAVED,
        HELPER if helper else ASSISTANT,
        name=artifact.name,
        file_kind=artifact.kind,
        artifact_id=artifact.id,
        version=artifact.version,
        helper_id=saver.id if helper else None,
    )


@dataclass(frozen=True)
class Stopped:
    """What a person's Stop came to.

    :param round_id: The Round it stops; only that Round's Helpers are interrupted.
    :param line: The Stop line for the Conversation's history; ``None`` when the Round had
        already settled, so nothing was stopped.
    """

    round_id: str
    line: str | None


async def stop_round(conversation: Any, live_helpers: Sequence[Any]) -> Stopped | None:
    """Stop a Conversation's Round (ADR 0029, "Stop stops the whole Round").

    The Round stopped is the Conversation's current one while it runs, else the newest Round
    one of *live_helpers* is still working in; others are left alone. ``stop.requested`` is
    recorded once, on that Round, and the Stop line is read from its folded record.

    :param conversation: The Conversation's row (a Helper's own Stop is not a Round's).
    :param live_helpers: The Conversation's Helpers still running.
    :returns: The Round and its line, or ``None`` outside a Round (every Helper is stopped,
        as before Rounds). Never raises.
    """
    current = round_of(conversation.labels)
    if RECORDER is None or current is None or conversation.kind == "sub_agent":
        return None
    try:
        rounds = [current]
        for helper in live_helpers:
            round_id = round_of(helper.labels)
            if round_id is not None and round_id not in rounds:
                rounds.append(round_id)
        views = {round_id: await RECORDER.view(round_id) for round_id in rounds}
        open_rounds = [r for r in rounds if views[r]["settled_at"] is None]
        if not open_rounds:
            return Stopped(current, None)
        target = (
            current
            if current in open_rounds
            else max(open_rounds, key=lambda r: views[r]["started_at"] or 0)
        )
        RECORDER.record(target, conversation.id, STOP_REQUESTED, PERSON, scope="round")
        return Stopped(target, (await RECORDER.view(target))["stop_line"])
    except Exception:
        _logger.warning("Stop not recorded for %s", conversation.id, exc_info=True)
        return Stopped(current, None)


async def stopped_wake(
    conversation_store: Any,
    conversation: Any,
    data: Mapping[str, Any],
    context: Mapping[str, Any] | None,
) -> bool:
    """Whether a message is a Helper's notice (its result, progress or flag) from a Round the
    person stopped. Stop ends a Round's pending wakes, so such a notice starts no turn.
    Never raises."""
    if RECORDER is None or _by_person(data) or not is_superside_chat(conversation.labels):
        return False
    try:
        _helper_id, round_id = await _notice_helper(conversation_store, conversation, context)
        return round_id is not None and (await RECORDER.view(round_id))["status"] == "stopped"
    except Exception:
        _logger.warning("wake of %s not checked against Stop", conversation.id, exc_info=True)
        return False


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            raw = json.loads(raw)
    return raw if isinstance(raw, dict) else {}


def _ok(output: Any) -> bool:
    """Whether a tool's output reads as success: not the stopped marker, not ``{"error"}``, not
    ``{"ok": false}`` (a ``deck_check`` that found errors)."""
    if output == STOPPED_OUTPUT:
        return False
    parsed = _args(output)
    return not parsed.get("error") and parsed.get("ok") is not False


@dataclass
class _Turn:
    round_id: str
    conversation_id: str
    actor: str
    helper_id: str | None
    text: list[str] = field(default_factory=list)
    #: The turn streamed words of its own.
    said: bool = False
    #: The ``message`` of the answer card the turn showed: its words when it wrote none.
    card_words: str = ""
    started_ms: int = field(default_factory=_now_ms)
    #: Ended from the store (:func:`close_lost_turns`): the relay records no more of it.
    closed: bool = False


#: ``session id -> its turn a relay records``, so a turn ended from the store is never ended twice.
_LIVE: dict[str, _Turn] = {}
#: ``session id -> the Round it opened`` until a turn of the session starts (:func:`note_message`).
_WAITING: dict[str, str] = {}
#: Running :meth:`RelayRound.lost` checks, kept so none is collected mid-run.
_CHECKS: set[asyncio.Task[None]] = set()


def open_turns(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The ``turn.started`` of each actor's turn (the Assistant's, or one Helper's) that no
    ``turn.ended`` followed yet."""
    started: dict[str | None, Mapping[str, Any]] = {}
    for ev in events:
        if ev["kind"] == TURN_STARTED:
            started[ev.get("helper_id")] = ev
        elif ev["kind"] == TURN_ENDED:
            started.pop(ev.get("helper_id"), None)
    return list(started.values())


def _replies(conversation_store: Any, session_id: str, since: float) -> list[str]:
    """The texts of the session's assistant messages stored at or after *since* (seconds)."""
    page = conversation_store.list_items(session_id, limit=_REPLIES, order="desc", type="message")
    texts = [
        _text({"content": item.data.content})
        for item in reversed(page.data)
        if item.created_at >= int(since) and getattr(item.data, "role", None) == "assistant"
    ]
    return [t for t in texts if t.strip()]


async def close_lost_turns(
    conversation_store: Any,
    round_id: str,
    conversation_id: str,
    *,
    before_ms: int,
    idle: Idle,
    assistant_session: str | None = None,
) -> bool:
    """End each open turn of *round_id* that started by *before_ms* and whose session is
    *idle*, from what that session stored, never from a guess.

    The turn's missing ``words`` are its stored assistant messages past what was streamed; its
    ``turn.ended`` reads ``done`` when it stored a reply, else ``failed`` with reason
    :data:`RELAY_LOST`. A Helper's turn also records ``helper.finished`` with its last reply.
    Idempotent: an ended turn is never ended again, here or by the relay. Never raises.

    :param conversation_id: The Conversation the Round is in; the Assistant's session.
    :param before_ms: Only turns started by then (epoch ms) are read as lost.
    :param idle: Whether a session (the Conversation, or a Helper) runs no turn.
    :param assistant_session: The session of the Assistant's turns when it is not the
        Conversation (a run no person started in a child session).
    :returns: Whether the Round still has an open turn.
    """
    if RECORDER is None:
        return False
    try:
        async with RECORDER.closing:
            events = await RECORDER.events(round_id)
            still_open = False
            for start in open_turns(events):
                helper_id = start.get("helper_id")
                session_id = helper_id or assistant_session or conversation_id
                if round(start["t"] * 1000) > before_ms or not await idle(session_id):
                    still_open = True
                    continue
                live = _LIVE.get(session_id)
                if live is not None and live.round_id == round_id and live.started_ms <= before_ms:
                    live.closed = True
                    del _LIVE[session_id]
                replies = await asyncio.to_thread(
                    _replies, conversation_store, session_id, start["t"]
                )
                _end_lost(round_id, conversation_id, start, events, replies)
            await RECORDER.idle()
            return still_open
    except Exception:
        _logger.warning("lost turns of round %s not closed", round_id, exc_info=True)
        return True


def _end_lost(
    round_id: str,
    conversation_id: str,
    start: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    replies: Sequence[str],
) -> None:
    actor, helper_id = start["actor"], start.get("helper_id")
    streamed = "".join(
        e.get("text") or ""
        for e in events
        if e["seq"] > start["seq"]
        and e["kind"] == WORDS
        and e["actor"] == actor
        and e.get("helper_id") == helper_id
    )
    stored = "".join(replies)
    if stored.startswith(streamed):
        missing = stored[len(streamed) :]
    else:
        missing = replies[-1] if replies and replies[-1] not in streamed else ""
    _logger.info("round %s: a %s turn the relay lost is ended from the store", round_id, actor)
    if missing:
        _record(round_id, conversation_id, WORDS, actor, text=missing, helper_id=helper_id)
    status = "done" if replies else "failed"
    _record(
        round_id,
        conversation_id,
        TURN_ENDED,
        actor,
        helper_id=helper_id,
        status=status,
        reason=None if replies else RELAY_LOST,
    )
    if actor == HELPER:
        result = clip(replies[-1].strip(), RESULT_LIMIT) if replies else ""
        _record(
            round_id,
            conversation_id,
            HELPER_FINISHED,
            actor,
            helper_id=helper_id,
            status=status,
            result=result,
        )


def _end_unturned(round_id: str, conversation_id: str, reason: str) -> None:
    """Settle an unsettled Round no turn will run for: the Assistant's part of it ends."""
    if _WAITING.get(conversation_id) == round_id:
        del _WAITING[conversation_id]
    _logger.info("round %s settled with no turn to come (%s)", round_id, reason)
    _record(round_id, conversation_id, TURN_ENDED, ASSISTANT, status="failed", reason=reason)


async def turn_failed(conversation_store: Any, session_id: str, reason: str) -> None:
    """A turn of *session_id* failed before reaching a runner (a cold host that never came up,
    a dispatch that failed), so no turn runs for what its current Round waits for: the Round
    ends now, failed with the failure as its reason, instead of reading "working" until the
    Sweep. Read from the store, so it holds across a restart. A turn a runner relayed ends from
    its own stream. Never raises."""
    if RECORDER is None:
        return
    try:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        round_id = round_of(conv.labels) if conv is not None else None
        if conv is None or round_id is None or _is_helper(conv):
            return
        if (await RECORDER.view(round_id))["turn_due"]:
            root = conv.root_conversation_id or conv.id
            _end_unturned(round_id, root, clip(reason, RESULT_LIMIT) if reason else NO_TURN)
    except Exception:
        _logger.warning("round of %s not settled after a failed turn", session_id, exc_info=True)


async def _results_read(conversation_id: str) -> None:
    """The Conversation's session went idle with nothing queued: each of its newest Rounds
    that waits only on Helper results steered into a turn now ended records ``session.idle``,
    so the fold counts those results read and the Round settles. Never raises."""
    if RECORDER is None:
        return
    try:
        for round_id in await RECORDER.recent(conversation_id, _IDLE_ROUNDS):
            if (await RECORDER.view(round_id))["read_on_idle"]:
                _record(round_id, conversation_id, SESSION_IDLE, SYSTEM)
    except Exception:
        _logger.warning("steered results of %s not read", conversation_id, exc_info=True)


async def reset_rounds(conversation_id: str) -> None:
    """A reset cleared the Conversation's context, so no turn will answer a Round of it still
    unsettled: each ends with ``turn.ended`` failed, reason :data:`RESET`. A Round with a Helper
    still working settles when the Helper ends. Never raises."""
    if RECORDER is None:
        return
    try:
        live = _LIVE.get(conversation_id)
        for round_id in await RECORDER.recent(conversation_id, _RESET_ROUNDS):
            if live is not None and live.round_id == round_id:
                continue  # its turn is still relayed and ends there
            if (await RECORDER.view(round_id))["settled_at"] is None:
                _end_unturned(round_id, conversation_id, RESET)
        await RECORDER.idle()
    except Exception:
        _logger.warning("rounds of %s not settled at reset", conversation_id, exc_info=True)


async def settle_stale(round_id: str, conversation_id: str, *, before_ms: int, idle: Idle) -> bool:
    """Settle *round_id*, with ``turn.ended`` failed reason :data:`NO_TURN`, when it is
    unsettled with no turn open, no Helper of it runs, it recorded nothing after *before_ms*
    and its Conversation is *idle*: its message never got a turn, or the wake that would read a
    Helper's result was lost.

    :returns: Whether the Round is still unsettled, so the :class:`Sweep` keeps reading it.
        Never raises.
    """
    if RECORDER is None:
        return False
    try:
        events = await RECORDER.events(round_id)
        view = fold(events, round_id)
        if view["settled_at"] is not None:
            return False
        if open_turns(events) or any(h["status"] == "running" for h in view["helpers"].values()):
            return True
        if view["read_on_idle"] and await idle(conversation_id):
            _record(round_id, conversation_id, SESSION_IDLE, SYSTEM)
            await RECORDER.idle()
            return False
        if not events or round(events[-1]["t"] * 1000) > before_ms:
            return True
        if not await idle(conversation_id):
            return True
        _end_unturned(round_id, conversation_id, NO_TURN)
        await RECORDER.idle()
        return False
    except Exception:
        _logger.warning("stale round %s not settled", round_id, exc_info=True)
        return True


class Sweep:
    """Every :data:`SWEEP_EVERY` seconds, end each turn the relay lost (:func:`close_lost_turns`)
    that has been open :data:`LOST_AFTER` seconds while its session is idle, and settle each
    Round no turn is coming for (:func:`settle_stale`).

    It reads the Rounds opened or a turn started in since the engine started (the recorder's
    ``turned``) and, at start, every Round the store holds from the last :data:`SWEEP_LOOKBACK`
    seconds, so a Round left unsettled (with or without a turn) is never forgotten across a
    restart; a Round leaves the list once it has no open turn and has settled.
    """

    def __init__(self, store: SqlAlchemyRoundStore, conversation_store: Any, idle: Idle) -> None:
        self._store = store
        self._conversations = conversation_store
        self._idle = idle
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the sweep on the running loop."""
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        """Stop the sweep, then let the recorder write what it holds and journal what settled."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if RECORDER is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(RECORDER.drain(), JOURNAL_DRAIN_S)

    async def _run(self) -> None:
        if RECORDER is None:
            return
        try:
            since = _now_ms() - int(SWEEP_LOOKBACK * 1000)
            left = await asyncio.to_thread(self._store.rounds_since, since)
            for round_id, conversation_id in left:
                RECORDER.turned.setdefault(round_id, conversation_id)
        except Exception:
            _logger.warning("recent rounds not read", exc_info=True)
        while True:
            await asyncio.sleep(SWEEP_EVERY)
            await self.once()

    async def once(self) -> None:
        """One pass over the Rounds a turn started in."""
        if RECORDER is None:
            return
        now = _now_ms()
        before = now - int(LOST_AFTER * 1000)
        stale = now - int(STALE_AFTER * 1000)
        for round_id, conversation_id in list(RECORDER.turned.items()):
            if await close_lost_turns(
                self._conversations, round_id, conversation_id, before_ms=before, idle=self._idle
            ):
                continue
            if not await settle_stale(round_id, conversation_id, before_ms=stale, idle=self._idle):
                RECORDER.turned.pop(round_id, None)


class RelayRound:
    """One session's runner stream, recorded as the events of the Round its turns are in.

    The server's relay hands it every frame (:meth:`observe`); a turn's Round is read from the
    session's label when the turn starts.
    """

    def __init__(self, session_id: str, conversation_store: Any) -> None:
        self._session_id = session_id
        self._store = conversation_store
        self._turn: _Turn | None = None
        #: ``False`` once the session reads as outside Superchat: its turns are never read again.
        self._superside: bool | None = None
        self._calls: dict[str, tuple[str, dict[str, Any]]] = {}
        self._called: set[str] = set()
        self._inputs: dict[str, list[float]] = {}

    async def observe(self, event: Mapping[str, Any], *, stopped: bool = False) -> None:
        """Record what *event* says happened, if this session's turn is in a Round.

        :param stopped: The engine stopped this turn. Its end reads ``stopped`` whatever the
            executor reported: a Helper interrupted while the executor called its turn done
            (the stop lost the race) is recorded stopped, never done.
        """
        if RECORDER is None:
            return
        kind = event.get("type")
        try:
            if kind == "response.in_progress":
                await self._begin()
            turn = self._turn
            if turn is not None and turn.closed:
                turn = self._turn = None
            if turn is None:
                return
            if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
                turn.text.append(event["delta"])
                turn.said = True
                RECORDER.words(
                    turn.round_id, turn.conversation_id, turn.actor, event["delta"], turn.helper_id
                )
            elif kind == TOOL_INPUT_DELTA:
                self._input(turn, event)
            elif kind == "response.output_item.done" and isinstance(event.get("item"), Mapping):
                self._item(turn, event["item"])
            elif kind in _TERMINAL:
                error = event.get("error")
                reason = error.get("message") if isinstance(error, Mapping) else None
                status = "stopped" if stopped else _TERMINAL[str(kind)]
                self._end(turn, status, clip(str(reason), RESULT_LIMIT) if reason else None)
        except Exception:
            _logger.warning("round event not recorded for %s", self._session_id, exc_info=True)

    async def _begin(self) -> None:
        self._turn = None
        _WAITING.pop(self._session_id, None)
        if self._superside is False:
            return
        conv = await asyncio.to_thread(self._store.get_conversation, self._session_id)
        if conv is not None:
            self._superside = is_superside_chat(conv.labels)
        round_id = round_of(conv.labels) if conv is not None else None
        if conv is None or round_id is None:
            self._turn = None
            return
        helper = _is_helper(conv)
        self._turn = _Turn(
            round_id,
            conv.root_conversation_id or conv.id,
            HELPER if helper else ASSISTANT,
            conv.id if helper else None,
        )
        self._calls, self._called, self._inputs = {}, set(), {}
        _LIVE[self._session_id] = self._turn
        self._emit(self._turn, TURN_STARTED)

    def lost(self) -> None:
        """End, from the store, this session's turn whose stream the relay lost: called when the
        session reports idle and when its relay ends for good. A no-op when the turn ended."""
        if RECORDER is None or self._superside is False:
            return
        before = _now_ms()
        session_id = self._session_id

        async def idle(candidate: str) -> bool:
            return candidate == session_id

        async def check() -> None:
            turn = self._turn
            if turn is not None:
                round_id, conversation_id = turn.round_id, turn.conversation_id
                helper = turn.actor == HELPER
            else:
                conv = await asyncio.to_thread(self._store.get_conversation, session_id)
                round_id = round_of(conv.labels) if conv is not None else None
                if conv is None or round_id is None:
                    return
                conversation_id = conv.root_conversation_id or conv.id
                helper = _is_helper(conv)
            await close_lost_turns(
                self._store,
                round_id,
                conversation_id,
                before_ms=before,
                idle=idle,
                assistant_session=None if helper else session_id,
            )
            if session_id == conversation_id:
                await _results_read(conversation_id)

        try:
            task = asyncio.get_running_loop().create_task(check())
        except RuntimeError:
            return
        _CHECKS.add(task)
        task.add_done_callback(_CHECKS.discard)

    def _emit(self, turn: _Turn, kind: str, **body: Any) -> None:
        if RECORDER is not None:
            RECORDER.record(
                turn.round_id,
                turn.conversation_id,
                kind,
                turn.actor,
                helper_id=turn.helper_id,
                **body,
            )

    def _started(self, turn: _Turn, call_id: str, name: str) -> None:
        if call_id not in self._calls:
            self._calls[call_id] = (name, {})
            self._emit(turn, TOOL_STARTED, call_id=call_id, name=name)

    def _input(self, turn: _Turn, event: Mapping[str, Any]) -> None:
        call_id, name = str(event.get("call_id") or ""), str(event.get("name") or "")
        if not call_id:
            return
        self._started(turn, call_id, name)
        size = self._inputs.setdefault(call_id, [0.0, 0.0])
        size[0] += len(str(event.get("delta") or "").encode())
        now = time.monotonic()
        if size[0] and now - size[1] >= INPUT_EVERY:
            size[1] = now
            self._emit(turn, TOOL_INPUT, call_id=call_id, name=name, bytes=int(size[0]))

    def _item(self, turn: _Turn, item: Mapping[str, Any]) -> None:
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        if item.get("type") == "function_call":
            name = str(item.get("name") or "")
            self._started(turn, call_id, name)
            if call_id not in self._called:
                self._called.add(call_id)
                args = _args(item.get("arguments"))
                self._calls[call_id] = (name, args)
                self._emit(
                    turn,
                    TOOL_CALLED,
                    call_id=call_id,
                    name=name,
                    digest=call_digest(name, args),
                    card=args.get("card") if name == CARD_TOOL_NAME else None,  # its kind
                )
            turn.text.clear()  # the words before a call are not the turn's last text
        elif item.get("type") == "function_call_output":
            name, args = self._calls.get(call_id, ("", {}))
            output = item.get("output")
            ok = _ok(output)
            self._emit(turn, TOOL_RESULT, call_id=call_id, name=name, ok=ok)
            if ok:
                self._effect(turn, name, args, output)

    def _effect(self, turn: _Turn, name: str, args: Mapping[str, Any], output: Any) -> None:
        """The chips, checklist and card words a tool call that worked made."""
        if name == CARD_TOOL_NAME and turn.actor == ASSISTANT:
            message = _args(output).get("message")
            if _args(output).get("card") in TERMINAL_CARDS and isinstance(message, str):
                turn.card_words = message
        elif name == _CHIPS_TOOL and turn.actor == ASSISTANT:
            items = args.get("items")
            if isinstance(items, list):
                self._emit(turn, CHIPS, items=[str(i) for i in items])
        elif name == _CHECKLIST_CREATE:
            number = _TASK_NUMBER.search(str(output or ""))
            self._emit(
                turn,
                CHECKLIST_CREATED,
                item_id=number.group(1) if number else None,
                subject=args.get("subject"),
                active_form=args.get("activeForm"),
            )
        elif name == _CHECKLIST_UPDATE:
            self._emit(
                turn,
                CHECKLIST_UPDATED,
                item_id=str(args.get("taskId") or "") or None,
                status=args.get("status"),
                subject=args.get("subject"),
                active_form=args.get("activeForm"),
            )
        elif name == CHECKLIST_TOOL:
            item_id = args.get("item_id")
            self._emit(
                turn,
                CHECKLIST_UPDATED if item_id else CHECKLIST_CREATED,
                item_id=str(item_id or _args(output).get("item_id") or "") or None,
                status=STATUSES.get(str(args.get("status"))),
                subject=args.get("subject"),
                active_form=args.get("active_form"),
            )

    def _end(self, turn: _Turn, status: str, reason: str | None = None) -> None:
        if _LIVE.get(self._session_id) is turn:
            del _LIVE[self._session_id]
        if turn.card_words and not turn.said and RECORDER is not None:
            # A turn that answered with a card and its `message` said those words.
            RECORDER.words(
                turn.round_id, turn.conversation_id, turn.actor, turn.card_words, turn.helper_id
            )
        self._emit(turn, TURN_ENDED, status=status, reason=reason if status == "failed" else None)
        if turn.actor == HELPER:
            result = clip("".join(turn.text).strip(), RESULT_LIMIT)
            self._emit(turn, HELPER_FINISHED, status=status, result=result)
        self._turn = None
