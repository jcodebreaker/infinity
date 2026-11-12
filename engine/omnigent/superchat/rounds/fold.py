"""The Round view is a fold over the Round's events (ADR 0029, principle 3). Pure: no store, no
clock, no I/O, and the view it returns shares nothing with the events it was given.

An event is a flat mapping: ``t`` (seconds), ``kind``, ``actor``, ``round_id`` and the kind's
fields (:mod:`omnigent.superchat.rounds.record` writes them). Settlement, Stop, a failed
Helper and events that arrive after settlement are cases of the fold, not rules beside it:

* A Round is **settled** when no Assistant turn runs, no Helper runs and no Helper result waits
  for the Assistant turn that reads it. Settlement is checked after every event and never moves
  once set.
* **Stop** settles the Round at once: every running Helper reads stopped, and the Stop line
  (:func:`stop_line`) names what was still working and what was saved.
* After settlement an event is kept in ``after_settle`` and changes nothing, except a
  ``turn.ended`` (it commits the words streamed before it) and a ``helper.finished`` (it keeps
  the late result on its Helper).
* An Assistant ``turn.ended`` with no Assistant turn running is the engine settling a Round no
  turn is coming for (a reset, or a Round left without one): no turn reads the Helper results
  it still holds.
* A Round that settles on an Assistant ``turn.ended`` whose status is ``failed`` (a model error,
  a turn the relay lost with no reply stored, a reset, a Round the sweep found with no turn to
  come) is **failed**, with that end's ``reason``; one that settles on any other end is done.
* A ``wake`` that came while a turn of the Conversation ran (``steered``; that turn may be
  another Round's) is read by that turn, or by the turn the runner runs right after it for the
  queued notice. ``session.idle`` (the Conversation's session went idle with nothing queued)
  says it was: the steered results count as read then, and the Round may settle.
* ``helper.titled`` renames a Helper (its model-written name), before or after settlement.
* Each actor has one live slot (the Assistant, or one Helper by its id): the newest tool it
  started. ``now`` is the slot started last; a settled Round has none. A delivery tool (a card,
  the follow-up chips, a question to the person) is not work and never takes a slot, nor is
  handing a job to a Helper (``start_helper``, ``message_helper``): the Helper's row names it.
* A Round has **delivered** (``delivered_at``) once the Assistant's turn showed its answer (a
  terminal card, the chips or a question) and no Helper of it runs or waits to be read; the
  Assistant starting more work, a new turn or a new Helper takes it back. It only says what to
  show: the Round still settles at its turn's end.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.superchat.cards.tools import (
    CARD_TOOL_NAME,
    CLARIFICATION_TOOL_NAME,
    FOLLOW_UPS_TOOL_NAME,
    TERMINAL_CARDS,
)
from omnigent.superchat.helpers.tools import MESSAGE_HELPER_TOOL_NAME, START_HELPER_TOOL_NAME

#: Event kinds (ADR 0029). ``record`` writes them; the fold reads them.
ROUND_OPENED = "round.opened"
TURN_STARTED, TURN_ENDED = "turn.started", "turn.ended"
WORDS = "words"
TOOL_STARTED, TOOL_INPUT, TOOL_CALLED, TOOL_RESULT = (
    "tool.started",
    "tool.input",
    "tool.called",
    "tool.result",
)
HELPER_STARTED, HELPER_FINISHED = "helper.started", "helper.finished"
HELPER_TITLED = "helper.titled"
FILE_SAVED = "file.saved"
CHIPS = "chips"
CHECKLIST_CREATED, CHECKLIST_UPDATED = "checklist.created", "checklist.updated"
WAKE = "wake"
SESSION_IDLE = "session.idle"
STOP_REQUESTED = "stop.requested"

#: Actors.
PERSON, SYSTEM, ASSISTANT, HELPER = "person", "system", "assistant", "helper"

#: Tools that show the person something rather than work on it: never the live tool.
_DELIVERY = frozenset({CARD_TOOL_NAME, CLARIFICATION_TOOL_NAME, FOLLOW_UPS_TOOL_NAME})
#: Tools that never take an actor's live slot: delivery, and handing a job to a Helper.
_NO_SLOT = _DELIVERY | {START_HELPER_TOOL_NAME, MESSAGE_HELPER_TOOL_NAME}

#: Events that still apply to a settled Round.
_AFTER_SETTLE = frozenset({TURN_ENDED, HELPER_FINISHED, HELPER_TITLED})


def _slot(ev: Mapping[str, Any]) -> str:
    """The live slot an event belongs to: the Assistant's, or its Helper's."""
    return str(ev.get("helper_id") or ev.get("actor") or ASSISTANT)


def _running(helpers: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [h for h in helpers.values() if h["status"] == "running"]


def _new_helper(helper_id: str, t: float) -> dict[str, Any]:
    return {
        "id": helper_id,
        "title": None,
        "folder": None,
        "status": "running",
        "started_at": t,
        "finished_at": None,
        "result": None,
    }


def _listed(names: Sequence[str]) -> str:
    quoted = [f"'{n}'" for n in names]
    return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def stop_line(working: Sequence[str | None], saved: Sequence[str]) -> str:
    """The Stop line: the one line the engine writes (ADR 0029), from a fixed template and only
    the Round's record, never a model call. It goes in the Conversation's history, so the
    Assistant's next turn can say honestly what finished, e.g.
    ``"Stopped. 'Uruguay deck' was still working; nothing was saved."``

    :param working: Titles of the Helpers still running at Stop.
    :param saved: Names of the files the Round saved.
    """
    clauses = []
    if working:
        titled = [t for t in working if t]
        named = len(titled) == len(working)
        who = _listed(titled) if named else "the work"
        clauses.append(f"{who} {'were' if named and len(titled) > 1 else 'was'} still working")
    if saved:
        clauses.append(f"{_listed(saved)} {'was' if len(saved) == 1 else 'were'} saved")
    else:
        clauses.append("nothing was saved")
    text = "; ".join(clauses)
    return f"Stopped. {text[0].upper()}{text[1:]}."


def fold(events: Sequence[Mapping[str, Any]], round_id: str | None = None) -> dict[str, Any]:
    """The view of one Round.

    :param events: Events in record order; only those of *round_id* are read.
    :param round_id: The Round to fold; ``None`` folds the Round of the last event.
    :returns: ``status`` (``idle``, ``running``, ``done``, ``failed`` or ``stopped``), ``hidden``
        (a run no person started), ``started_at``, ``settled_at``, ``elapsed``, ``ask``,
        ``reason`` (why a failed Round failed, when its end said), ``stop_line``,
        ``now`` (the live tool or ``None``), ``trail``, ``helpers`` by id, ``files``, ``chips``,
        ``checklist``, ``words`` (one entry per finished Assistant turn), ``live_words`` (the
        running turn's text so far), ``failed_helper``, ``after_settle``, ``turn_due`` (an
        unsettled Round waits for an Assistant turn that has not started: its message's first
        turn, or the wake that reads a Helper's result), ``read_on_idle`` (an unsettled Round
        whose only wait is Helper results steered into a turn that has ended: the session going
        idle settles it), ``delivered_at`` (when the Round delivered its answer, or ``None``)
        and ``trace_id`` (the Round's OpenTelemetry trace, from its ``round.opened``, or
        ``None``).
    """
    if round_id is None and events:
        round_id = events[-1].get("round_id")
    # Read, never changed: what the view keeps of an event (``after_settle``, ``chips``) is a copy.
    mine = [e for e in events if e.get("round_id") == round_id]
    v: dict[str, Any] = {
        "status": "idle",
        "hidden": False,
        "started_at": None,
        "settled_at": None,
        "ask": None,
        "reason": None,
        "stop_line": None,
        "now": None,
        "trail": [],
        "helpers": {},
        "files": [],
        "chips": [],
        "checklist": [],
        "words": [],
        "live_words": "",
        "after_settle": [],
        "failed_helper": False,
        "delivered_at": None,
        "trace_id": None,
    }
    assistant_running = False
    unconsumed: set[str] = set()
    steered: set[str] = set()  # results in *unconsumed* whose wake was steered into a turn
    turned = False  # an Assistant turn started
    cur_words: list[str] | None = None
    live: dict[str, dict[str, Any]] = {}
    ended: Mapping[str, Any] | None = None  # the last Assistant turn.ended
    cards: dict[str, Any] = {}  # call id -> the kind of card it showed
    digests: dict[str, str] = {}  # call id -> the digest of a call that takes no slot
    delivered: float | None = None  # when the Assistant's turn showed its answer
    for ev in mine:
        kind, t, actor = ev["kind"], ev["t"], ev.get("actor")
        settled = v["settled_at"] is not None
        if settled:
            v["after_settle"].append(copy.deepcopy(dict(ev)))
            if kind not in _AFTER_SETTLE:
                continue
        if kind == ROUND_OPENED:
            if v["status"] == "idle":
                v.update(
                    status="running",
                    started_at=t,
                    hidden=actor == SYSTEM,
                    ask=ev.get("text"),
                    trace_id=ev.get("trace_id"),
                )
                assistant_running, cur_words = True, []
        elif kind == WORDS:
            if actor == ASSISTANT:
                if cur_words is None:  # a late chunk of the turn that just ended
                    if v["words"]:
                        v["words"][-1] += ev["text"]
                    else:
                        v["words"].append(ev["text"])
                else:
                    cur_words.append(ev["text"])
        elif kind in (TOOL_STARTED, TOOL_CALLED) and ev.get("name") in _NO_SLOT:
            if ev.get("card"):
                cards[str(ev.get("call_id"))] = ev["card"]
            if ev.get("digest"):
                digests[str(ev.get("call_id"))] = ev["digest"]
        elif kind in (TOOL_STARTED, TOOL_CALLED):
            slot = _slot(ev)
            if slot == ASSISTANT:
                delivered = None  # the Assistant works on
            cur = live.get(slot)
            if kind == TOOL_STARTED or cur is None or cur["call_id"] != ev.get("call_id"):
                live.pop(slot, None)  # the newest tool of an actor supersedes its last one
                cur = live[slot] = {
                    "actor": actor,
                    "helper_id": ev.get("helper_id"),
                    "call_id": ev.get("call_id"),
                    "name": ev.get("name"),
                    "since": t,
                    "bytes": 0,
                    "digest": None,
                }
            if kind == TOOL_CALLED:
                cur["digest"] = ev.get("digest")
        elif kind == TOOL_INPUT:
            cur = live.get(_slot(ev))
            if cur and cur["call_id"] == ev.get("call_id"):
                cur["bytes"] = ev.get("bytes") or 0
        elif kind == TOOL_RESULT:
            slot = _slot(ev)
            cur = live.get(slot)
            digest = digests.get(str(ev.get("call_id")))
            if cur and cur["call_id"] == ev.get("call_id"):
                digest = cur["digest"]
                del live[slot]
            v["trail"].append(
                {
                    "t": t,
                    "call_id": ev.get("call_id"),
                    "actor": actor,
                    "helper_id": ev.get("helper_id"),
                    "name": ev.get("name"),
                    "digest": digest,
                    "ok": bool(ev.get("ok", True)),
                }
            )
            name, card = ev.get("name"), cards.get(str(ev.get("call_id")))
            answer = name in (CLARIFICATION_TOOL_NAME, FOLLOW_UPS_TOOL_NAME) or (
                name == CARD_TOOL_NAME and card in TERMINAL_CARDS
            )
            if slot == ASSISTANT and answer and ev.get("ok", True):
                delivered = delivered or t
        elif kind == FILE_SAVED:
            name = ev.get("name")
            v["files"] = [f for f in v["files"] if f["name"] != name]  # a re-save replaces it
            v["files"].append(
                {
                    "name": name,
                    "kind": ev.get("file_kind"),
                    "artifact_id": ev.get("artifact_id"),
                    "version": ev.get("version"),
                    "helper_id": ev.get("helper_id"),
                }
            )
        elif kind == CHIPS:
            v["chips"] = copy.deepcopy(list(ev.get("items") or []))
        elif kind == CHECKLIST_CREATED:
            item_id = ev.get("item_id")
            v["checklist"] = [i for i in v["checklist"] if i["id"] != item_id]
            v["checklist"].append(
                {
                    "id": item_id,
                    "subject": ev.get("subject"),
                    "active_form": ev.get("active_form"),
                    "status": ev.get("status") or "pending",
                }
            )
        elif kind == CHECKLIST_UPDATED:
            for item in v["checklist"]:
                if item["id"] == ev.get("item_id"):
                    item.update(
                        {k: ev[k] for k in ("subject", "active_form", "status") if ev.get(k)}
                    )
            v["checklist"] = [i for i in v["checklist"] if i["status"] != "deleted"]
        elif kind == HELPER_STARTED:
            h = v["helpers"].setdefault(ev["helper_id"], _new_helper(ev["helper_id"], t))
            h["title"] = ev.get("title") or h["title"]  # never reopens a finished Helper
            h["folder"] = ev.get("folder") or h["folder"]
            delivered = None
        elif kind == HELPER_TITLED:
            h = v["helpers"].get(ev.get("helper_id"))
            if h is not None and ev.get("title"):
                h["title"] = ev["title"]
        elif kind == HELPER_FINISHED:
            h = v["helpers"].get(ev["helper_id"])
            if settled:
                if h:
                    h["result"] = ev.get("result")
            elif h is None or h["status"] == "running":  # unknown id: finished arrived first
                h = v["helpers"].setdefault(ev["helper_id"], _new_helper(ev["helper_id"], t))
                h.update(status=ev.get("status") or "done", finished_at=t, result=ev.get("result"))
                unconsumed.add(ev["helper_id"])
                live.pop(ev["helper_id"], None)
        elif kind == WAKE:
            if ev.get("steered") and ev.get("helper_id") in unconsumed:
                steered.add(ev["helper_id"])
        elif kind == SESSION_IDLE:
            if not assistant_running:
                unconsumed -= steered  # the turn they were steered into read them
                steered = set()
        elif kind == TURN_STARTED and actor == ASSISTANT:
            assistant_running, cur_words, turned = True, [], True
            delivered = None
            unconsumed, steered = set(), set()  # an Assistant turn reads the results that woke it
        elif kind == TURN_ENDED and actor == ASSISTANT:
            if not assistant_running:
                unconsumed, steered = set(), set()  # no turn running: no turn will read them
            assistant_running, ended = False, ev
            if cur_words:
                v["words"].append("".join(cur_words))
            cur_words = None
            live.pop(ASSISTANT, None)
        elif kind == STOP_REQUESTED and not settled:
            v.update(status="stopped", settled_at=t)
            working = [h["title"] for h in _running(v["helpers"])]
            v["stop_line"] = stop_line(working, [f["name"] for f in v["files"]])
            for h in _running(v["helpers"]):
                h.update(status="stopped", finished_at=t)
        if (
            v["settled_at"] is None
            and v["status"] == "running"
            and not assistant_running
            and not _running(v["helpers"])
            and not unconsumed
        ):
            failed = any(h["status"] == "failed" for h in v["helpers"].values())
            v.update(status="done", settled_at=t, failed_helper=failed)
            if ended is not None and ended.get("status") == "failed":
                v.update(status="failed", reason=ended.get("reason"))
        if v["settled_at"] is not None:
            live = {}  # nothing is live on a settled Round
        if v["settled_at"] is None:
            v["delivered_at"] = (
                delivered if not _running(v["helpers"]) and not unconsumed else None
            )
    v["now"] = copy.deepcopy(list(live.values())[-1]) if live else None
    v["live_words"] = "".join(cur_words or []) if v["settled_at"] is None else ""
    unsettled = v["status"] == "running"
    v["turn_due"] = unsettled and (
        (assistant_running and not turned)
        or (not assistant_running and bool(unconsumed - steered))
    )
    v["read_on_idle"] = unsettled and not assistant_running and bool(steered)
    end = v["settled_at"] if v["settled_at"] is not None else (mine[-1]["t"] if mine else 0)
    v["elapsed"] = round(end - (v["started_at"] or 0), 1) if v["started_at"] is not None else 0
    return v


def summary(view: Mapping[str, Any]) -> dict[str, Any]:
    """The few fields of a Round *view* a list row or a live signal carries.

    :returns: ``status``, ``started_at``, ``settled_at``, ``delivered_at``, ``live`` (the live
        tool's digest, else its name, else ``None``), and how many ``helpers``, ``files`` and
        finished ``steps`` it has. The times say on their own whether the Round's strip is live,
        so a client can settle it from the signal before it reads the view again.
    """
    now = view.get("now")
    return {
        "status": view["status"],
        "started_at": view["started_at"],
        "settled_at": view["settled_at"],
        "delivered_at": view["delivered_at"],
        "live": (now.get("digest") or now.get("name")) if now else None,
        "helpers": len(view["helpers"]),
        "files": len(view["files"]),
        "steps": len(view["trail"]),
    }
