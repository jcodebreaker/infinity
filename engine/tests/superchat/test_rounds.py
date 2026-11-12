"""A Round is recorded and folded (ADR 0029): the fold over the recorded streams and its cases,
the Round rule, and the writers from a relayed stream to the ``round_events`` table."""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.context.labels import (
    ARTIFACT_CHAT_LABEL_KEY,
    CONTEXT_MODE_LABEL,
    ROUND_LABEL_KEY,
    SCHEDULED_FIRE_LABEL_KEY,
    SCHEDULED_HELPER_LABEL_KEY,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.entities.conversation import ConversationItem, MessageData
from omnigent.superchat.rounds import record
from omnigent.superchat.rounds.fold import fold
from omnigent.superchat.rounds.store import SqlAlchemyRoundStore

ROUNDS = Path(__file__).parent / "rounds"
RANK = {"idle": 0, "running": 1, "done": 2, "failed": 2, "stopped": 2}


def load(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (ROUNDS / f"{name}.jsonl").read_text().splitlines()]


class S:
    """A Round's event stream with a clock that moves one second per event."""

    def __init__(self, rid: str = "r1") -> None:
        self.ev: list[dict[str, Any]] = []
        self.t = 0.0
        self.rid = rid
        self.n = 0

    def __call__(self, kind: str, actor: str = "assistant", dt: float = 1.0, **body: Any) -> S:
        self.t = round(self.t + dt, 3)
        self.ev.append({"t": self.t, "kind": kind, "round_id": self.rid, "actor": actor, **body})
        return self

    def open(self) -> S:
        return self("round.opened", "person", text="x")("turn.started")

    def started(self, tool: str, helper: str | None = None) -> S:
        self.n += 1
        return self("tool.started", **_by(helper), call_id=f"c{self.n}", name=tool)

    def inp(self, tool: str, helper: str | None = None, size: int = 2048) -> S:
        return self("tool.input", **_by(helper), call_id=f"c{self.n}", name=tool, bytes=size)

    def result(self, tool: str, helper: str | None = None, ok: bool = True, dt: float = 1.0) -> S:
        return self("tool.result", **_by(helper), dt=dt, call_id=f"c{self.n}", name=tool, ok=ok)

    def save(self, name: str, helper: str | None = None, dt: float = 1.0) -> S:
        self.started("artifact_save", helper).result("artifact_save", helper, dt=dt)
        return self("file.saved", **_by(helper), dt=0.0, name=name, file_kind="deck")

    def spawn(self, hid: str, title: str) -> S:
        self.started("start_helper")("helper.started", helper_id=hid, title=title)
        return self.result("start_helper")

    def words(self, text: str, helper: str | None = None) -> S:
        return self("words", **_by(helper), text=text)

    def end(self) -> S:
        return self("turn.ended", status="done")

    def wake(self) -> S:
        return self("turn.started")

    def fin(self, hid: str, status: str = "done", result: str = "ok") -> S:
        return self("helper.finished", "helper", helper_id=hid, status=status, result=result)

    def stop(self) -> S:
        return self("stop.requested", "person", scope="round")


def _by(helper: str | None) -> dict[str, str]:
    """Who an event is from: the Assistant, or the Helper with that id."""
    return {"actor": "helper", "helper_id": helper} if helper else {"actor": "assistant"}


def cuts_ok(events: list[dict[str, Any]], rid: str = "r1") -> dict[str, Any]:
    """Fold every cut: same output for the same input, status never goes down, settlement never
    moves, files never shrink, a settled Round shows nothing live and no running Helper."""
    prev: dict[str, Any] | None = None
    for i in range(len(events) + 1):
        v = fold(events[:i], rid)
        assert v == fold(events[:i], rid)
        if prev is not None:
            assert RANK[v["status"]] >= RANK[prev["status"]], (i, prev["status"], v["status"])
            if prev["settled_at"] is not None:
                assert (v["settled_at"], v["status"]) == (prev["settled_at"], prev["status"]), i
            assert len(v["files"]) >= len(prev["files"]), i
        if v["settled_at"] is not None:
            assert v["now"] is None, (i, v["now"])
            assert not any(h["status"] == "running" for h in v["helpers"].values()), i
        prev = v
    assert prev is not None
    return prev


# ------------------------------------------------------------------ the recorded streams
@pytest.mark.parametrize(
    ("name", "status", "files", "helper"),
    [
        ("claude_A_normal", "done", ["uruguay"], "done"),
        ("pi_A_normal", "done", ["uruguay"], "done"),
        ("claude_B_stop", "stopped", [], "stopped"),
        ("pi_B_stop", "stopped", [], "stopped"),
    ],
)
def test_recorded_stream_folds_on_every_cut(
    name: str, status: str, files: list[str], helper: str, db_uri: str
) -> None:
    events = load(name)
    v = cuts_ok(events)
    assert v["status"] == status
    assert [f["name"] for f in v["files"]] == files
    assert v["helpers"]["h1"]["status"] == helper
    assert v["words"] and v["words"][0].startswith("I")
    if status == "stopped":
        # The executor said the stopped Helper was done (Claude); the Round says stopped.
        title = v["helpers"]["h1"]["title"]
        assert v["stop_line"] == f"Stopped. '{title}' was still working; nothing was saved."
        follow_up = fold(events, "r2")
        assert follow_up["status"] == "done" and follow_up["helpers"] == {}
    # Reconnecting mid-turn: the first turn's streamed words are in the view before it ends.
    first_end = next(i for i, e in enumerate(events) if e["kind"] == "turn.ended")
    assert "Uruguay" in fold(events[:first_end], "r1")["live_words"]
    # Written through the Recorder into a real table and read back, each Round folds the same.
    store = SqlAlchemyRoundStore(db_uri)
    recorder = record.Recorder(store)
    ids = {"r1": "1" * 32, "r2": "2" * 32}

    async def write() -> None:
        for e in events:
            body = {k: v for k, v in e.items() if k not in ("t", "round_id", "kind", "actor")}
            recorder.record(ids[e["round_id"]], "c" * 32, e["kind"], e["actor"], **body)
        await recorder.idle()

    asyncio.run(write())
    for rid, stored_id in ids.items():
        want, got = fold(events, rid), fold(store.events(stored_id), stored_id)
        for key in ("status", "stop_line", "files", "chips", "words", "checklist"):
            assert got[key] == want[key], (rid, key)
        assert {h: x["status"] for h, x in got["helpers"].items()} == {
            h: x["status"] for h, x in want["helpers"].items()
        }


# ------------------------------------------------------------------ the fold's cases
def _two_helpers() -> S:
    s = S().open().spawn("h1", "A").spawn("h2", "B").words("Working on both.").end()
    return s.started("artifact_save", "h2").inp("artifact_save", "h2", 1024)


def _full_run() -> S:
    s = S().open().words("ok").spawn("h1", "Deck").end()
    s.save("d", "h1").fin("h1").wake().words("Ready.")
    s.started("suggest_follow_ups").result("suggest_follow_ups")("chips", items=["a"])
    return s.end()


def _stop_race() -> tuple[dict[str, Any], dict[str, Any]]:
    s = S().open().spawn("h1", "Deck").end()
    s.started("artifact_save", "h1").stop()
    before = fold(s.ev, "r1")
    s("tool.result", "helper", dt=0.01, helper_id="h1", call_id=f"c{s.n}", ok=True)
    s("file.saved", "helper", dt=0.0, helper_id="h1", name="d").fin("h1", result="saved d")
    return before, cuts_ok(s.ev)


def _redelivered(kind: str, after: str) -> dict[str, Any]:
    ev = _full_run().ev
    i = next(i for i, e in enumerate(ev) if e["kind"] == kind)
    j = next(
        j for j, e in enumerate(ev) if j > i and e["kind"] == after and e["actor"] != "person"
    )
    return fold([*ev[: j + 1], dict(ev[i], t=ev[j]["t"]), *ev[j + 1 :]], "r1")


def _late_events_change_nothing() -> list[str]:
    base = S().open().spawn("h1", "Deck").end().stop()
    v0 = fold(base.ev, "r1")
    late: list[dict[str, Any]] = [
        {"kind": "tool.started", "actor": "helper", "helper_id": "h1", "call_id": "x"},
        {"kind": "helper.started", "actor": "assistant", "helper_id": "h9", "title": "Late"},
        {"kind": "round.opened", "actor": "person", "text": "x"},
        {"kind": "turn.started", "actor": "assistant"},
        {"kind": "file.saved", "actor": "helper", "helper_id": "h1", "name": "late"},
        {"kind": "checklist.created", "actor": "assistant", "item_id": "1", "subject": "a"},
    ]
    bad = []
    for e in late:
        v = fold([*base.ev, {"t": base.t + 1, "round_id": "r1", **e}], "r1")
        same = {k: x for k, x in v.items() if k != "after_settle"} == {
            k: x for k, x in v0.items() if k != "after_settle"
        }
        if not same or len(v["after_settle"]) != len(v0["after_settle"]) + 1:
            bad.append(e["kind"])
    return bad


def _view_does_not_alias_input() -> bool:
    s = S().open()("checklist.created", item_id="1", subject="a").end().stop()
    s("file.saved", "helper", helper_id="h1", name="late")
    snapshot = copy.deepcopy(s.ev)
    v = fold(s.ev, "r1")
    v["checklist"].append("mutated")
    for e in v["after_settle"]:
        e["kind"] = "mutated"
    return s.ev == snapshot


CASES: list[tuple[str, Callable[[], Any], Callable[[Any], bool]]] = [
    ("empty", lambda: fold([]), lambda v: v["status"] == "idle" and v["elapsed"] == 0),
    (
        "direct build settles done",
        lambda: cuts_ok(S().open().save("d").words("Here.").end().ev),
        lambda v: v["status"] == "done" and v["words"] == ["Here."] and len(v["files"]) == 1,
    ),
    (
        "words only from the Assistant, chips from their event",
        lambda: fold(S().open().words("hi").words("helper", "h1")("chips", items=["a"]).end().ev),
        lambda v: v["words"] == ["hi"] and v["chips"] == ["a"] and v["status"] == "done",
    ),
    (
        "a re-save replaces the file",
        lambda: fold(S().open().save("d").save("d").end().ev),
        lambda v: [f["name"] for f in v["files"]] == ["d"],
    ),
    (
        "two Helpers, one wake",
        lambda: cuts_ok(_two_helpers().fin("h1").fin("h2").wake().words("Both.").end().ev),
        lambda v: v["status"] == "done" and v["words"][-1] == "Both.",
    ),
    (
        "two Helpers, the second finishes during the first wake",
        lambda: fold(_two_helpers().fin("h1").wake().fin("h2").end().ev),
        lambda v: v["status"] == "running",
    ),
    (
        "a second-generation Helper is waited for",
        lambda: fold(S().open().spawn("h1", "R").end().fin("h1").wake().spawn("h2", "D").end().ev),
        lambda v: v["status"] == "running",
    ),
    (
        "a failed Helper waits for its wake, then done and failed",
        lambda: cuts_ok(
            S().open().spawn("h1", "D").end().fin("h1", "failed", "boom").wake().end().ev
        ),
        lambda v: (
            v["status"] == "done" and v["failed_helper"] and v["helpers"]["h1"]["result"] == "boom"
        ),
    ),
    (
        "a Round whose last turn failed reads failed with its reason",
        lambda: cuts_ok(
            S().open().words("On it")("turn.ended", status="failed", reason="boom").ev
        ),
        lambda v: (v["status"], v["reason"], v["words"]) == ("failed", "boom", ["On it"]),
    ),
    (
        "a failed turn its wake turn followed reads done",
        lambda: cuts_ok(
            S()
            .open()
            .spawn("h1", "D")("turn.ended", status="failed", reason="boom")
            .fin("h1")
            .wake()
            .end()
            .ev
        ),
        lambda v: (v["status"], v["reason"]) == ("done", None),
    ),
    (
        "the titler renames a Helper, after settlement too; an unknown Helper is not added",
        lambda: cuts_ok(
            S()
            .open()
            .spawn("h1", "worker task")
            .end()
            .fin("h1")
            .wake()
            .end()("helper.titled", helper_id="h1", title="Faroe deck")(
                "helper.titled", helper_id="h9", title="Ghost"
            )
            .ev
        ),
        lambda v: (
            v["status"] == "done"
            and v["helpers"]["h1"]["title"] == "Faroe deck"
            and list(v["helpers"]) == ["h1"]
        ),
    ),
    (
        "a Helper result is not consumed before its wake",
        lambda: fold(S().open().spawn("h1", "D").end().fin("h1").ev),
        lambda v: v["status"] == "running" and not v["failed_helper"],
    ),
    (
        "helper.finished before helper.started",
        lambda: fold(
            S()
            .open()
            .fin("h1")("helper.started", dt=0.01, helper_id="h1", title="D")
            .end()
            .wake()
            .end()
            .ev
        ),
        lambda v: v["status"] == "done" and v["helpers"]["h1"]["status"] == "done",
    ),
    (
        "Stop races a save 10 ms after it",
        _stop_race,
        lambda r: (
            r[1]["status"] == "stopped"
            and r[1]["settled_at"] == r[0]["settled_at"]
            and r[1]["files"] == []
            and r[1]["trail"] == r[0]["trail"]
            and r[1]["helpers"]["h1"]["result"] == "saved d"
        ),
    ),
    (
        "Stop while the Assistant writes keeps only the words before it",
        lambda: cuts_ok(
            S().open().words("I'll make").started("start_helper").stop().words(" a deck").end().ev
        ),
        lambda v: v["stop_line"] == "Stopped. Nothing was saved." and v["words"] == ["I'll make"],
    ),
    (
        "a stopped Round shows nothing live",
        lambda: fold(S().open().spawn("h1", "D").end().started("artifact_save", "h1").stop().ev),
        lambda v: v["now"] is None,
    ),
    (
        "two Stops are one",
        lambda: (
            fold(S().open().spawn("h1", "D").end().stop().ev),
            fold(S().open().spawn("h1", "D").end().stop().stop().ev),
        ),
        lambda r: (
            (r[0]["settled_at"], r[0]["stop_line"]) == (r[1]["settled_at"], r[1]["stop_line"])
        ),
    ),
    (
        "the Stop line never says nothing was saved when a file was",
        lambda: fold(S().open().spawn("h1", "D").end().save("d", "h1").stop().ev),
        lambda v: v["stop_line"] == "Stopped. 'D' was still working; 'd' was saved.",
    ),
    (
        "a reopen after Stop never runs again",
        lambda: fold(S().open().spawn("h1", "D").end().stop()("round.opened", "person").ev),
        lambda v: v["status"] == "stopped",
    ),
    (
        "Stop before the Round opens",
        lambda: fold(S().stop()("round.opened", "person").ev),
        lambda v: not (v["status"] == "running" and v["settled_at"] is not None),
    ),
    ("events after settlement change nothing", _late_events_change_nothing, lambda bad: bad == []),
    (
        "a result without a start, and input after a result",
        lambda: fold(
            S()
            .open()
            .result("deck_check")
            .started("artifact_save")
            .result("artifact_save")
            .inp("artifact_save")
            .ev
        ),
        lambda v: v["now"] is None and len(v["trail"]) == 2,
    ),
    (
        "the Assistant's tool does not take a Helper's live line",
        lambda: fold(
            _two_helpers()
            .fin("h1")
            .wake()
            .started("suggest_follow_ups")
            .result("suggest_follow_ups")
            .end()
            .ev
        ),
        lambda v: (
            v["now"] is not None and v["now"]["helper_id"] == "h2" and v["now"]["bytes"] == 1024
        ),
    ),
    (
        "a word after its turn ended is kept",
        lambda: fold(S().open().spawn("h1", "D").words("Building.").end().words(" (late)").ev),
        lambda v: v["status"] == "running" and v["words"] == ["Building. (late)"],
    ),
    (
        "every event delivered twice",
        lambda: (
            fold(_full_run().ev),
            fold(
                [
                    x
                    for e in _full_run().ev
                    for x in ([e, dict(e)] if e["kind"] != "words" else [e])
                ]
            ),
        ),
        lambda r: all(
            r[0][k] == r[1][k]
            for k in ("status", "settled_at", "files", "chips", "helpers", "words")
        ),
    ),
    (
        "helper.started redelivered after its finish",
        lambda: _redelivered("helper.started", "helper.finished"),
        lambda v: v["status"] == "done" and v["helpers"]["h1"]["status"] == "done",
    ),
    (
        "helper.finished redelivered during the wake",
        lambda: _redelivered("helper.finished", "turn.started"),
        lambda v: v["status"] == "done",
    ),
    (
        "Rounds are folded apart",
        lambda: (S("r1").open().spawn("h1", "D").end().ev, S("r2").open().words("other").end().ev),
        lambda r: fold(sorted(r[0] + r[1], key=lambda e: e["t"]), "r1") == fold(r[0], "r1"),
    ),
    ("the view never aliases its events", _view_does_not_alias_input, lambda same: same),
    (
        "a hidden Round is a system run",
        lambda: fold(S()("round.opened", "system", text="notes")("turn.started").end().ev),
        lambda v: v["hidden"] and v["status"] == "done",
    ),
    (
        "a deck is checked by its Helper's clean deck_check, the check line reads it",
        lambda: fold(
            S()
            .open()
            .spawn("h1", "Deck")
            .end()
            .save("a.deck.html", "h1")
            .result("deck_check", "h1", ok=False)
            .result("deck_check", "h1")
            .fin("h1")
            .ev
        ),
        lambda v: record.checked(v) is True,
    ),
    (
        "a deck with no clean check is not checked; a Round with no deck has no check line",
        lambda: (
            fold(S().open().save("a.deck.html").result("deck_check", ok=False).end().ev),
            fold(S().open().save("notes.md").end().ev),
        ),
        lambda r: record.checked(r[0]) is False and record.checked(r[1]) is None,
    ),
]


@pytest.mark.parametrize(("build", "check"), [pytest.param(b, c, id=n) for n, b, c in CASES])
def test_fold_case(build: Callable[[], Any], check: Callable[[Any], bool]) -> None:
    assert check(build())


KINDS = [
    "words",
    "tool.started",
    "tool.input",
    "tool.result",
    "file.saved",
    "helper.started",
    "helper.finished",
    "turn.started",
    "turn.ended",
    "stop.requested",
    "checklist.created",
    "round.opened",
    "helper.titled",
]


def _random_event(rng: random.Random, t: float) -> dict[str, Any]:
    kind = rng.choice(KINDS)
    hid = rng.choice(["h1", "h2"])
    actor = rng.choice(["assistant", "helper"])
    e: dict[str, Any] = {"t": t, "kind": kind, "round_id": rng.choice(["r1", "r1", "r1", "r2"])}
    e["actor"] = actor
    if actor == "helper":
        e["helper_id"] = hid
    if kind == "words":
        e["text"] = rng.choice(["a", "b "])
    elif kind.startswith("tool."):
        e.update(
            call_id=rng.choice(["c1", "c2"]), name="artifact_save", bytes=rng.randint(0, 9000)
        )
    elif kind == "file.saved":
        e["name"] = rng.choice(["d", "p"])
    elif kind == "turn.ended":
        e.update(status=rng.choice(["done", "failed"]), reason=rng.choice([None, "boom"]))
    elif kind in ("helper.started", "helper.finished", "helper.titled"):
        e.update(helper_id=hid, title="T", status=rng.choice(["done", "failed", "stopped"]))
    elif kind == "checklist.created":
        e.update(item_id="1", subject="x")
    return e


def test_random_streams_keep_the_invariants() -> None:
    """300 seeded streams of any kind, either actor, either Round, in odd orders."""
    for seed in range(300):
        rng = random.Random(seed)
        ev = [{"t": 0.0, "kind": "round.opened", "round_id": "r1", "actor": "person"}]
        ev += [_random_event(rng, round(0.1 * (i + 1), 2)) for i in range(rng.randint(1, 40))]
        cuts_ok(ev)


# ------------------------------------------------------------------ the Round rule
SUPER = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}
IN_ROUND = {**SUPER, ROUND_LABEL_KEY: "r0"}


@pytest.mark.parametrize(
    ("labels", "helper", "by_person", "running", "helper_round", "expected"),
    [
        pytest.param({}, False, True, False, None, None, id="not a Superchat session"),
        pytest.param(SUPER, False, True, False, None, ("m1", True, "person"), id="person opens"),
        pytest.param(IN_ROUND, False, True, False, None, ("m1", True, "person"), id="next ask"),
        pytest.param(IN_ROUND, False, True, True, None, ("r0", False, "person"), id="steer joins"),
        pytest.param(IN_ROUND, False, False, False, "rh", ("rh", False, "person"), id="wake"),
        pytest.param(IN_ROUND, False, False, True, None, ("r0", False, "person"), id="notice"),
        pytest.param(SUPER, False, False, False, None, None, id="notice outside a Round"),
        pytest.param(
            IN_ROUND, True, True, False, None, ("r0", False, "person"), id="Helper brief"
        ),
        pytest.param(SUPER, True, True, False, None, None, id="Helper outside a Round"),
        pytest.param(
            {**SUPER, SCHEDULED_FIRE_LABEL_KEY: "s1"},
            False,
            True,
            False,
            None,
            ("m1", True, "system"),
            id="scheduled run is hidden",
        ),
    ],
)
def test_assign_round(
    labels: dict[str, str],
    helper: bool,
    by_person: bool,
    running: bool,
    helper_round: str | None,
    expected: tuple[str, bool, str] | None,
) -> None:
    got = record.assign_round(
        item_id="m1",
        labels=labels,
        helper=helper,
        by_person=by_person,
        turn_running=running,
        helper_round=helper_round,
    )
    assert (got and (got.round_id, got.opened, got.actor)) == expected


# ------------------------------------------------------------------ the writers
class _Conversations:
    """The calls the writers make on the conversation store."""

    def __init__(self, *rows: types.SimpleNamespace) -> None:
        self.rows = {row.id: row for row in rows}
        self.replies: dict[str, list[str]] = {}

    def list_items(self, conversation_id: str, **_page: Any) -> types.SimpleNamespace:
        """The session's stored assistant messages, newest first."""
        data = MessageData(role="assistant", content=[], agent="a")
        items = [
            ConversationItem(
                id=f"m{i}",
                type="message",
                status="completed",
                response_id="resp",
                created_at=int(time.time()),
                data=data.model_copy(update={"content": [{"type": "output_text", "text": t}]}),
            )
            for i, t in enumerate(self.replies.get(conversation_id, []))
        ]
        return types.SimpleNamespace(data=items[::-1])

    def get_conversation(self, conversation_id: str) -> types.SimpleNamespace | None:
        return self.rows.get(conversation_id)

    def set_labels(self, conversation_id: str, labels: dict[str, str]) -> None:
        self.rows[conversation_id].labels.update(labels)


def _row(cid: str, labels: dict[str, str], parent: str | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=cid,
        root_conversation_id=parent or cid,
        parent_conversation_id=parent,
        kind="sub_agent" if parent else "default",
        labels=dict(labels),
        title="worker:Deck" if parent else "Deck",
        sub_agent_name="worker" if parent else None,
        workspace="/home/someone/workspace/projects/q3" if parent else "/home/someone/workspace",
    )


#: The Helper's tool calls in the recorded Round, and the digest each one is recorded with.
_HELPER_CALLS = [
    ("sys_os_shell", '{"command": "cd ~/workspace/your_files; ls; grep -r deck ."}', "ls"),
    ("Write", '{"file_path": "your_files/notes.md", "content": "Faroe notes"}', "notes.md"),
    (
        "deck_new",
        '{"path": "your_files/faroe.deck.html", "title": "Faroe Islands"}',
        "Faroe Islands",
    ),
    ("deck_theme_set", '{"path": "your_files/faroe.deck.html", "theme_id": "ink"}', "faroe"),
    ("mcp__omnigent__web_search", '{"query": "Faroe Islands", "limit": 5}', "Faroe Islands"),
    ("deck_export", '{"artifact_id": "v1", "format": "pdf"}', None),
]


def _done(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.done", "item": item}


def test_a_round_is_written_from_the_message_the_relay_and_the_save(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person's message opens the Round, its Helper inherits it, the relayed streams of both
    become events in order, the save and the Stop are recorded, each stored batch is handed on,
    and the fold reads them back. The Helper the Stop interrupts reads stopped though its
    executor called the turn done, and its late wake is a stopped Round's. The Stop settles it:
    one journal entry, never a second, and none for a hidden Round. The titler's name for the
    Helper, saved from a worker thread after settlement, still renames it in the view; a turn
    that fails ends with the error it failed with, and its Round reads failed."""
    store = SqlAlchemyRoundStore(db_uri)
    written: list[Any] = []
    journaled: list[tuple[str, dict[str, Any]]] = []

    async def sink(conversation_id: str, entry: dict[str, Any]) -> None:
        journaled.append((conversation_id, entry))

    monkeypatch.setattr(
        record, "RECORDER", record.Recorder(store, on_written=written.extend, on_settled=sink)
    )
    root = "a" * 32
    item = "b" * 32
    helper_id = "c" * 32
    ask = "d" * 32
    convs = _Conversations(_row(root, SUPER))
    msg = {"role": "user", "content": [{"type": "input_text", "text": "Make a deck."}]}
    assistant = record.RelayRound(root, convs)

    async def run() -> None:
        await record.note_message(convs, convs.rows[root], item, msg, None, turn_running=False)
        await assistant.observe({"type": "response.in_progress"})
        for delta in ("On ", "it."):
            await assistant.observe({"type": "response.output_text.delta", "delta": delta})
        await assistant.observe(
            {"type": record.TOOL_INPUT_DELTA, "call_id": "t1", "name": "start_helper", "delta": ""}
        )
        inherited = record.helper_labels(convs.rows[root].labels, SUPER)
        child = _row(helper_id, {**SUPER, **inherited}, root)
        convs.rows[helper_id] = child
        record.helper_created(child)
        call = {"type": "function_call", "call_id": "t1", "name": "start_helper"}
        brief = '{"title": "Deck", "task": "Build a 6-slide deck on the Faroe Islands."}'
        await assistant.observe(_done({**call, "arguments": brief}))
        out = {"type": "function_call_output", "call_id": "t1", "output": '{"started": true}'}
        await assistant.observe(_done(out))
        await assistant.observe({"type": "response.completed"})
        helper = record.RelayRound(helper_id, convs)
        await helper.observe({"type": "response.in_progress"})
        await helper.observe({"type": "response.output_text.delta", "delta": "Saved the deck."})
        for n, (tool, args, _) in enumerate(_HELPER_CALLS):
            fn = {"type": "function_call", "call_id": f"h{n}", "name": tool, "arguments": args}
            await helper.observe(_done(fn))
            await helper.observe(_done({"type": "function_call_output", "call_id": f"h{n}"}))
        record.file_saved(
            child, types.SimpleNamespace(name="deck", kind="html", id="v1", version=1)
        )
        stopped = await record.stop_round(convs.rows[root], [child])
        assert stopped == record.Stopped(
            item, "Stopped. 'Deck' was still working; 'deck' was saved."
        )
        await helper.observe({"type": "response.completed"}, stopped=True)
        wake = {"role": "user", "is_system_notice": True, "content": []}
        notice = {"helper_id": helper_id}
        assert await record.stopped_wake(convs, convs.rows[root], wake, notice)
        assert not await record.stopped_wake(convs, convs.rows[root], msg, None)
        assert record.RECORDER is not None
        await record.RECORDER.idle()
        await helper.observe({"type": "response.in_progress"})  # settled: a late turn ends
        await helper.observe({"type": "response.cancelled"})
        hidden = _row("e" * 32, {**SUPER, SCHEDULED_FIRE_LABEL_KEY: "s1"})
        convs.rows[hidden.id] = hidden
        await record.note_message(convs, hidden, "f" * 32, msg, None, turn_running=False)
        system = record.RelayRound(hidden.id, convs)
        await system.observe({"type": "response.in_progress"})
        await system.observe({"type": "response.completed"})
        # The next ask comes while the session still reads busy: the Stop settled its Round,
        # so the ask opens its own, never steering the stopped one. It waits on a cold host;
        # the Helper's late notice lands first. The turn the host then runs is the ask's, and
        # a steer during it joins it.
        await record.note_message(convs, convs.rows[root], ask, msg, None, turn_running=True)
        await record.note_message(
            convs, convs.rows[root], "9" * 32, wake, notice, turn_running=False
        )
        cold = record.RelayRound(root, convs)
        await cold.observe({"type": "response.in_progress"})
        await record.note_message(convs, convs.rows[root], "8" * 32, msg, None, turn_running=True)
        await cold.observe({"type": "response.failed", "error": {"message": "Overloaded"}})
        child.task_summary = "Faroe Islands deck"
        await asyncio.to_thread(record.helper_titled, child)
        await asyncio.sleep(0)
        await record.RECORDER.idle()

    asyncio.run(run())
    tools = [
        e for e in store.events(item) if e["actor"] == "helper" and e["kind"] == "tool.called"
    ]
    assert [e.get("digest") for e in tools] == [digest for *_, digest in _HELPER_CALLS]
    events = [e for e in store.events(item) if e["actor"] != "helper" or "tool" not in e["kind"]]
    events = events[:14]
    assert [(e["kind"], e["actor"]) for e in events] == [
        ("round.opened", "person"),
        ("turn.started", "assistant"),
        ("words", "assistant"),
        ("tool.started", "assistant"),
        ("helper.started", "assistant"),
        ("tool.called", "assistant"),
        ("tool.result", "assistant"),
        ("turn.ended", "assistant"),
        ("turn.started", "helper"),
        ("words", "helper"),
        ("file.saved", "helper"),
        ("stop.requested", "person"),
        ("turn.ended", "helper"),
        ("helper.finished", "helper"),
    ]
    seqs = [e["seq"] for e in store.events(item)]
    assert seqs == list(range(1, len(seqs) + 1))
    stored = [e["kind"] for e in store.events(item)]
    assert [e.kind for e in written if e.round_id == item] == stored  # handed on once stored
    assert events[2]["text"] == "On it." and events[5]["digest"] == "Deck"
    # The Helper's model title, its agent beside it, its folder relative to the workspace.
    assert {k: events[4][k] for k in ("title", "agent", "folder")} == {
        "title": "Deck",
        "agent": "worker",
        "folder": "projects/q3",
    }
    assert events[13]["status"] == "stopped" and events[13]["helper_id"] == helper_id
    view = fold(events, item)
    assert view["status"] == "stopped" and view["ask"] == "Make a deck."
    assert [f["name"] for f in view["files"]] == ["deck"]
    assert [(c, e["round_id"]) for c, e in journaled] == [(root, item), (root, ask)]
    entry = journaled[0][1]
    assert entry == record.journal_entry(item, view)
    keys = ("round_id", "ask", "outcome", "stopped", "files", "helpers")
    assert {k: entry[k] for k in keys} == {
        "round_id": item,
        "ask": "Make a deck.",
        "outcome": "On it.",
        "stopped": True,
        "files": ["deck"],
        "helpers": ["Deck"],
    }
    assert entry["folder"] == "projects/q3"
    assert entry["refs"] == {"artifacts": ["v1@v1"], "project": ["q3"], "helpers": [helper_id]}
    assert entry["checked"] is None  # no deck saved, no check run
    assert [(e["kind"], e["actor"]) for e in store.events(ask)] == [
        ("round.opened", "person"),
        ("turn.started", "assistant"),
        ("turn.ended", "assistant"),
    ]
    asked = fold(store.events(ask), ask)
    assert (asked["status"], asked["reason"]) == ("failed", "Overloaded")
    later = store.events(item)
    assert [e["kind"] for e in later][-3:] == ["helper.finished", "wake", "helper.titled"]
    assert fold(later, item)["helpers"][helper_id]["title"] == "Faroe Islands deck"
    hidden_view = fold(store.events("f" * 32))
    assert hidden_view["hidden"] is True and hidden_view["settled_at"] is not None
    assert convs.rows[helper_id].labels[ROUND_LABEL_KEY] == item
    # A scheduled Helper and a file's studio are not in their parent's Round.
    assert record.helper_labels(IN_ROUND, {SCHEDULED_HELPER_LABEL_KEY: "s1"}) == {}
    assert record.helper_labels(IN_ROUND, {ARTIFACT_CHAT_LABEL_KEY: "g1"}) == {}


class _Events:
    """A recorder that keeps the events it is handed, in order."""

    def __init__(self) -> None:
        self.ev: list[dict[str, Any]] = []

    def record(self, round_id: str, conversation_id: str, kind: str, actor: str, **body: Any):
        body = {k: v for k, v in body.items() if v is not None}
        self.ev.append(
            {"t": len(self.ev), "kind": kind, "round_id": round_id, "actor": actor, **body}
        )

    def words(self, *_args: Any) -> None:
        pass


async def _native_checklist(_root: str) -> tuple[list[tuple[str, dict[str, Any], str]], list[str]]:
    """Claude's task tools: the CLI numbers the items and names the number in its result."""
    return [
        ("TaskCreate", {"subject": "Deck", "activeForm": "Building"}, "Task #1 created: Deck"),
        ("TaskCreate", {"subject": "Page"}, "Task #2 created: Page"),
        ("TaskCreate", {"subject": "Old"}, "Task #3 created: Old"),
        ("TaskUpdate", {"taskId": "1", "status": "completed"}, "Updated task #1 status"),
        ("TaskUpdate", {"taskId": "2", "status": "in_progress"}, "Updated task #2 status"),
        ("TaskUpdate", {"taskId": "3", "status": "deleted"}, "Updated task #3 deleted"),
    ], ["1", "2"]


async def _tool_checklist(root: str) -> tuple[list[tuple[str, dict[str, Any], str]], list[str]]:
    """The ``checklist`` tool: the runner's handler names the items; a refused call adds none."""
    from omnigent.superchat.feature import HandlerCtx
    from omnigent.superchat.rounds.checklist import handle_checklist

    ctx = HandlerCtx("checklist", None, root)
    calls: list[tuple[str, dict[str, Any], str]] = []

    async def call(args: dict[str, Any]) -> str:
        output = await handle_checklist(ctx, args)
        calls.append(("checklist", args, output))
        return str(json.loads(output).get("item_id"))

    deck = await call({"subject": "Deck", "active_form": "Building"})
    page = await call({"subject": "Page"})
    await call({"status": "doing"})  # no subject, no item: refused
    await call({"item_id": deck, "status": "done"})
    await call({"item_id": page, "status": "doing"})
    return calls, [deck, page]


@pytest.mark.parametrize("source", [_native_checklist, _tool_checklist], ids=["native", "tool"])
def test_the_checklist_reads_the_same_from_either_tool(
    source: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model's checklist is its words beside the facts: Claude's task tools and the
    ``checklist`` tool are recorded as one event shape, so the view reads the same."""
    events = _Events()
    monkeypatch.setattr(record, "RECORDER", events)
    root = "a" * 32
    relay = record.RelayRound(root, _Conversations(_row(root, IN_ROUND)))

    async def run() -> list[str]:
        calls, ids = await source(root)
        await relay.observe({"type": "response.in_progress"})
        for n, (name, args, output) in enumerate(calls):
            call = {"type": "function_call", "call_id": f"t{n}", "name": name}
            await relay.observe(_done({**call, "arguments": json.dumps(args)}))
            await relay.observe(
                _done({"type": "function_call_output", "call_id": f"t{n}", "output": output})
            )
        await relay.observe({"type": "response.completed"})
        return ids

    deck, page = asyncio.run(run())
    assert fold(events.ev, "r0")["checklist"] == [
        {"id": deck, "subject": "Deck", "active_form": "Building", "status": "completed"},
        {"id": page, "subject": "Page", "active_form": None, "status": "in_progress"},
    ]


@pytest.mark.parametrize(
    ("by", "stored"),
    [
        pytest.param("relay", True, id="relay-idle-stored"),
        pytest.param("sweep", False, id="sweep-nothing-stored"),
        pytest.param("helper", True, id="sweep-helper"),
        pytest.param("reset", False, id="reset-no-turn"),
        pytest.param("stale", False, id="sweep-no-turn"),
    ],
)
def test_a_turn_the_relay_lost_ends_from_the_store(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, by: str, stored: bool
) -> None:
    """The relay's stream is cut after ``turn.started`` (the runner tunnel dropped mid-turn) and
    the session goes idle. The turn ends from what the session stored: the stored reply past the
    streamed words and ``done``, or ``failed`` with reason "relay lost" when nothing was stored.
    The Round settles, the relay's late terminal ends nothing twice, and a second pass is a
    no-op. The sweep closes a Helper's lost turn the same way, with its last reply as its
    result, which then waits for the wake turn that reads it, and the sweep keeps reading that
    Round until it settles. A Round whose message never got a turn settles too: at once when
    the Conversation is reset, or from the sweep once it has gone without one while idle."""
    store = SqlAlchemyRoundStore(db_uri)
    monkeypatch.setattr(record, "RECORDER", record.Recorder(store))
    monkeypatch.setattr(record, "LOST_AFTER", 0.0)
    if by == "stale":
        monkeypatch.setattr(record, "STALE_AFTER", 0.0)
    root, item, helper_id = "a" * 32, "b" * 32, "c" * 32
    convs = _Conversations(_row(root, SUPER))
    msg = {"role": "user", "content": [{"type": "input_text", "text": "Two things."}]}
    if stored:
        convs.replies[root] = ["Both are ", "finished."]

    async def idle(_session_id: str) -> bool:
        return True

    async def run() -> None:
        await record.note_message(convs, convs.rows[root], item, msg, None, turn_running=False)
        assert record.RECORDER is not None
        if by in ("reset", "stale"):  # the session is interrupted before the turn starts
            await record.RECORDER.idle()
            await asyncio.sleep(0.002)
            for _ in range(2):
                if by == "reset":
                    await record.reset_rounds(root)
                else:
                    await record.Sweep(store, convs, idle).once()
            assert item not in record.RECORDER.turned or by == "reset"
            return
        assistant = record.RelayRound(root, convs)
        await assistant.observe({"type": "response.in_progress"})
        await assistant.observe({"type": "response.output_text.delta", "delta": "Both "})
        if by == "helper":
            child = _row(helper_id, {**SUPER, ROUND_LABEL_KEY: item}, root)
            convs.rows[helper_id] = child
            record.helper_created(child)
            convs.replies[helper_id] = ["Saved the page."]
            await record.RelayRound(helper_id, convs).observe({"type": "response.in_progress"})
        await record.RECORDER.idle()
        await asyncio.sleep(0.002)  # the turns started before the pass reads them as lost
        for _ in range(2):
            if by == "relay":
                assistant.lost()
                await asyncio.gather(*record._CHECKS)
            else:
                await record.Sweep(store, convs, idle).once()
        await assistant.observe({"type": "response.completed"})  # a late terminal: ended already
        await record.RECORDER.idle()
        # A Round leaves the sweep's list once settled; the Helper's waits for its wake turn.
        assert (item in record.RECORDER.turned) == (by != "sweep")

    asyncio.run(run())
    events = store.events(item)
    if by in ("reset", "stale"):
        assert [(e["kind"], e["status"], e["reason"]) for e in events[1:]] == [
            ("turn.ended", "failed", record.RESET if by == "reset" else record.NO_TURN)
        ]
        view = fold(events, item)
        assert view["settled_at"] is not None
        assert (view["status"], view["reason"]) == ("failed", events[-1]["reason"])
        return
    ended = [e for e in events if e["kind"] == "turn.ended"]
    assert len(ended) == (2 if by == "helper" else 1)
    mine = next(e for e in ended if e["actor"] == "assistant")
    assert mine["status"] == ("done" if stored else "failed")
    assert mine.get("reason") == (None if stored else record.RELAY_LOST)
    view = fold(events, item)
    assert view["words"] == (["Both are finished."] if stored else ["Both "])
    if by == "helper":
        assert view["helpers"][helper_id]["status"] == "done"
        assert view["helpers"][helper_id]["result"] == "Saved the page."
        assert view["status"] == "running"  # settlement waits for the wake turn
    else:
        assert view["settled_at"] is not None
        assert (view["status"], view["reason"]) == (
            ("done", None) if stored else ("failed", record.RELAY_LOST)
        )
