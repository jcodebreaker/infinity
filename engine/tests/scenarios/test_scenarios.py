"""ADR 0025 scenarios and ADR 0029 Rounds, offline: no turn stalls, asks stay bounded, no reply
names the machinery."""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from omnigent.superchat.tasks import record
from scripts import scenarios as runner
from tests.scenarios.rules import (
    Event,
    as_turn,
    asks,
    checked,
    ends_well,
    gives_up,
    is_deliverable_turn,
    mechanism_words,
    speaks_first,
)
from tests.scenarios.scripts import ROUNDS, SCENARIOS, Scenario

_ALL = SCENARIOS + ROUNDS


def run(scenario: Scenario) -> list[list[Event]]:
    """A scenario's turns as the scripted model plays them."""
    return [list(turn) for turn in scenario.turns]


def test_the_adrs_name_ten_scenarios_and_six_rounds() -> None:
    assert len({scenario.name for scenario in SCENARIOS}) == 10
    assert len({scenario.name for scenario in ROUNDS}) == 6
    assert len({scenario.name for scenario in _ALL}) == 16


@pytest.mark.parametrize("scenario", _ALL, ids=lambda s: s.name)
def test_a_live_run_gives_a_deck_or_page_a_fresh_subject(scenario: Scenario) -> None:
    # reset leaves the host's files, so a repeat run would find the last run's deck
    played = runner.with_run_token(scenario, "ab12")
    for script, ran in zip(scenario.turns, played.turns, strict=True):
        (kind, text), (_, sent) = script[0], ran[0]
        fresh = bool(scenario.deliverable) and kind == "request"
        assert sent == (f"{text} (run ab12)" if fresh else text)
        assert ran[1:] == script[1:]


@pytest.mark.parametrize("scenario", _ALL, ids=lambda s: s.name)
def test_scenario_never_stalls(scenario: Scenario) -> None:
    turns = run(scenario)
    stalls = [index for index, turn in enumerate(turns) if not ends_well(turn)]
    assert stalls == [], turns
    assert not [
        text for turn in turns for kind, text in turn if kind == "reply" and gives_up(text)
    ]
    said = [
        w
        for turn in turns
        for kind, text in turn
        if kind == "reply"
        for w in mechanism_words(text)
    ]
    assert said == [], said
    asked = asks(turns)
    assert len(asked) <= scenario.max_asks
    assert len(asked) == len(set(asked)), f"asked again: {asked}"


@pytest.mark.parametrize(
    "scenario",
    [s for s in _ALL if any(is_deliverable_turn(turn) for turn in s.turns)],
    ids=lambda s: s.name,
)
def test_deliverable_scenario_speaks_first(scenario: Scenario) -> None:
    silent = [index for index, turn in enumerate(run(scenario)) if not speaks_first(turn)]
    assert silent == []


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS if s.deliverable == "deck"], ids=lambda s: s.name
)
def test_a_deck_scenario_ends_on_a_check_that_ran(scenario: Scenario) -> None:
    events = [event for turn in run(scenario) for event in turn]
    assert checked(events), events


@pytest.mark.parametrize(
    ("span", "ok"),
    [
        # the Helper built, saved and checked
        (
            [
                ("request", "a deck"),
                ("tool_call", "context_brief"),
                ("helper_started", ""),
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
            ],
            True,
        ),
        ([("request", "a deck"), ("tool_call", "deck_new"), ("save", "")], False),  # not checked
        # a save after the check needs another check
        (
            [("request", "a deck"), ("save", ""), ("tool_call", "deck_check"), ("save", "")],
            False,
        ),
        ([("request", "a deck"), ("tool_call", "deck_new"), ("cancelled", "")], True),
        ([("message", "time in Tokyo?"), ("tool_call", "web_search")], True),
        ([("request", "a deck"), ("card", "look")], True),  # nothing built
    ],
)
def test_checked(span: list[Event], ok: bool) -> None:
    assert checked(span) is ok


# The conversation in ADR 0025's opening: four turns, nothing built.
_ACME_VS_GLOBEX: list[list[Event]] = [
    [("request", "Create some presentation about Acme vs Globex"), ("card", "look")],
    [
        ("answer", "Pitch Deck"),
        ("reply", "I haven't built it yet, so I'll stop here for now."),
        ("card", "data"),
    ],
    [("answer", "Mock data"), ("card", "look")],
    [
        ("answer", "Bold Signal"),
        (
            "reply",
            "My last attempt didn't register your choice; send me a short message to continue.",
        ),
    ],
]


def test_the_adr_transcript_stalls_gives_up_and_asks_again() -> None:
    # the last answer ends with no file, card or Helper: the engine logs a stall
    assert [ends_well(turn) for turn in _ACME_VS_GLOBEX] == [True, True, True, False]
    replies = [[text for kind, text in turn if kind == "reply"] for turn in _ACME_VS_GLOBEX]
    assert [any(gives_up(text) for text in turn) for turn in replies] == [False, True, False, True]
    assert asks(_ACME_VS_GLOBEX) == ["look", "data", "look"]


def test_the_scenarios_judge_a_turn_with_the_engines_rule() -> None:
    turn = as_turn([("answer", "Nord"), ("reply", "Noted.")])
    assert not record.ends_well(turn) and not ends_well([("answer", "Nord"), ("reply", "Noted.")])


# The turn in ADR 0026's opening: the first output was the deck_new call, no word for 182 s.
_ACME_VS_GLOBEX_SILENT: list[Event] = [
    ("request", "create some presentation about acme vs globex"),
    ("tool_call", "deck_new"),
    ("tool_call", "deck_check"),
    ("save", ""),
    ("reply", "Here is the Acme vs Globex comparison."),
]


def test_the_adr_0026_transcript_does_not_speak_first() -> None:
    assert ends_well(_ACME_VS_GLOBEX_SILENT)
    assert not speaks_first(_ACME_VS_GLOBEX_SILENT)


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("Here is the Acme vs Globex comparison.", []),
        ("I started a Helper to build the deck.", ["helper"]),
        ("Two sub-agents and a worker are on it.", ["sub-agents", "worker"]),
        ("The harness stopped context_brief.", ["harness", "context_brief"]),
        ("Our helpers' coworker is helpful.", ["helpers"]),
    ],
)
def test_mechanism_words(text: str, said: list[str]) -> None:
    assert mechanism_words(text) == said


@pytest.mark.parametrize(
    ("turn", "spoken"),
    [
        ([("request", "a deck"), ("reply", "Making it."), ("tool_call", "deck_new")], True),
        ([("request", "a deck"), ("reply", "On it."), ("tool_call", "deck_new")], True),
        ([("request", "a deck"), ("helper_started", ""), ("reply", "On it.")], False),
        ([("request", "a deck"), ("card", "look")], True),
        ([("answer", "Nord"), ("tool_call", "deck_theme_set")], False),
        ([("message", "time in Tokyo?"), ("tool_call", "web_search")], True),
    ],
)
def test_speaks_first(turn: list[Event], spoken: bool) -> None:
    assert speaks_first(turn) is spoken


@pytest.mark.parametrize(
    ("turn", "well"),
    [
        # a request opens no task until its work does, so it never stalls
        ([("request", "a deck"), ("reply", "Working on it.")], True),
        ([("request", "a deck"), ("save", "")], True),
        ([("answer", "Nord"), ("save", "")], True),
        ([("answer", "Nord"), ("helper_started", "")], True),
        ([("answer", "Nord"), ("card", "data")], True),
        ([("answer", "Nord"), ("reply", "Noted.")], False),
        ([("answer", "Nord"), ("tool_call", "deck_new"), ("reply", "Building.")], False),
        ([("steer", "add pricing"), ("message_helper", ""), ("reply", "Noted.")], True),
        ([("message", "what time is it?"), ("reply", "9 pm.")], True),
        ([("notice", "progress"), ("reply", "Two sources read.")], True),
        ([("request", "a deck"), ("tool_call", "sys_os_bash"), ("cancelled", "")], True),
    ],
)
def test_ends_well(turn: list[Event], well: bool) -> None:
    assert ends_well(turn) is well


_SENT = 1000.0
_NOW = 1200.0


def _view(status: str, trail: list[tuple[float, str, str | None]], files: int) -> dict[str, Any]:
    """A Round view in the real schema's shape (``RoundView``): *trail* as (t, name, helper)."""
    done = status in ("done", "stopped")
    return {
        "round_id": "m1",
        "status": status,
        "hidden": False,
        "started_at": _SENT,
        "settled_at": 1090.0 if done else None,
        "elapsed": 90.0,
        "trail": [
            {
                "t": t,
                "actor": "helper" if helper else "assistant",
                "helper_id": helper,
                "name": name,
                "digest": None,
                "ok": True,
            }
            for t, name, helper in trail
        ],
        "helpers": {"h1": {"id": "h1", "status": "done", "started_at": 1010.0}}
        if any(helper for _, _, helper in trail)
        else {},
        "files": [{"name": "deck.html", "kind": "html", "artifact_id": "a1", "version": 1}]
        * files,
    }


# The live Round of round-deck-direct: the Assistant built it, deck_new twice, one file.
_DIRECT = [
    (1020.0, "deck_new", None),
    (1030.0, "deck_new", None),
    (1040.0, "sys_os_edit", None),
    (1050.0, "deck_check", None),
    (1060.0, "artifact_save", None),
]


@pytest.mark.parametrize(
    ("view", "deck", "columns"),
    [
        (_view("done", _DIRECT, 1), 20.0, {"settled": "1/1", "helpers": 0, "duration": 90.0}),
        # a Helper built it: its first deck_new, any actor
        (
            _view(
                "done",
                [(1010.0, "start_helper", None), (1040.0, "decks__deck_new", "h1")],
                1,
            ),
            40.0,
            {"settled": "1/1", "helpers": 1, "duration": 90.0},
        ),
        # a file with no deck tool in the trail ends the deck when it is seen
        (
            _view("running", [(1005.0, "sys_os_bash", None)], 1),
            _NOW - _SENT,
            {"settled": "0/1", "helpers": 0, "duration": 90.0},
        ),
        (
            _view("running", [(1005.0, "sys_os_bash", None)], 0),
            None,
            {"settled": "0/1", "helpers": 0, "duration": 90.0},
        ),
        (None, None, {"settled": "0/1", "helpers": 0, "duration": None}),
    ],
)
def test_the_runner_reads_the_deck_end_and_columns_from_the_round(
    view: dict[str, Any] | None, deck: float | None, columns: dict[str, Any]
) -> None:
    assert runner.deck_end(view or {}, _SENT, _NOW) == deck
    assert runner.round_columns([view]) == columns


@pytest.mark.parametrize(
    ("views", "deck", "polls"),
    [
        ([_view("done", _DIRECT, 1)], 20.0, 1),
        ([_view("running", [], 0), _view("running", _DIRECT[:1], 0)], 20.0, 2),
        ([_view("done", [(1005.0, "sys_os_bash", None)], 0)], None, 1),  # settled, no file
        ([_view("running", [], 0)], "stalled", 3),  # never ends: the deadline stops it
    ],
)
def test_the_deck_wait_ends_at_the_rounds_deck_end_or_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
    views: list[dict[str, Any]],
    deck: float | str | None,
    polls: int,
) -> None:
    clock = iter(range(100))
    monkeypatch.setattr(runner.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    seen: list[str | None] = []

    class Rounds:
        def round(self, session_id: str, item_id: str | None) -> dict[str, Any]:
            seen.append(item_id)
            return views[min(len(seen), len(views)) - 1]

    wait = runner._wait_for_deck
    if deck == "stalled":
        with pytest.raises(runner.Stalled):
            wait(Rounds(), "s1", "m1", _SENT, 3.0)  # type: ignore[arg-type]
    else:
        assert wait(Rounds(), "s1", "m1", _SENT, 3.0) == deck  # type: ignore[arg-type]
    assert seen == ["m1"] * polls


def test_a_turn_ends_the_deck_at_deck_new() -> None:
    items = [
        {"id": "m1", "type": "message", "role": "user", "created_at": 1000},
        {"type": "message", "role": "assistant", "content": [], "created_at": 1001},
        {"type": "function_call", "name": "decks__deck_new", "created_at": 1030},
    ]
    assert runner.waits(items)["deck"] == 30.0
    assert runner._message_id(items) == "m1"


@pytest.mark.parametrize(("method", "calls"), [("GET", 2), ("POST", 1)])
def test_the_runner_tries_a_hung_get_once_more(
    monkeypatch: pytest.MonkeyPatch, method: str, calls: int
) -> None:
    # a POST may have landed before its timeout, so only a GET is sent again
    timeouts: list[float] = []

    def hang(request: object, timeout: float) -> None:
        timeouts.append(timeout)
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(runner.urllib.request, "urlopen", hang)
    with pytest.raises(urllib.error.URLError):
        runner.Engine("http://engine.invalid", "token").call(method, "/v1/me")
    assert timeouts == [10.0] * calls


@pytest.mark.parametrize(
    ("session", "harness", "calls", "runs"),
    [
        (None, None, [("GET", "/v1/me/muse", None)], "claude-sdk"),
        (None, "pi", [("PUT", "/v1/me/muse", {"agent": "superchat-pi"})], "pi"),
        ("s2", None, [], "pi"),
    ],
)
def test_a_scenario_past_its_deadline_is_stalled_on_the_conversation_it_targets(
    monkeypatch: pytest.MonkeyPatch,
    session: str | None,
    harness: str | None,
    calls: list[tuple[str, str, dict[str, str] | None]],
    runs: str,
) -> None:
    class Hung:
        """An engine whose turn never settles; the account's Conversation is ``s1``."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, str] | None]] = []

        def call(self, method: str, path: str, body: dict[str, str] | None = None) -> Any:
            self.calls.append((method, path, body))
            return {"session_id": "s1"} if path == "/v1/me/muse" else {"harness": runs}

        def items(self, session_id: str) -> list[dict[str, Any]]:
            return [{"id": "m1", "type": "message", "role": "user", "created_at": 1}]

        def activities(self, session_id: str) -> list[dict[str, Any]]:
            return []

        def status(self, session_id: str) -> str:
            return "running"

        def send(self, session_id: str, text: str) -> None:
            return None

        def round(self, session_id: str, item_id: str | None) -> None:
            return None

    engine = Hung()
    # The harness comes from the bundles, the way the web maps it (engine/models.ts).
    assert runner.harness_agents() == {"claude-sdk": "superchat-claude", "pi": "superchat-pi"}
    session_id, harness_now = runner.target(engine, session, harness)  # type: ignore[arg-type]
    assert session_id == (session or "s1") and harness_now == runs
    assert engine.calls[:-1] == calls
    assert engine.calls[-1][1].startswith(f"/v1/sessions/{session_id}?")
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    row = runner.play(engine, session_id, ROUNDS[0], deadline=0.0)  # type: ignore[arg-type]
    assert row["stalled"] and row["settled"] == "0/0"
    assert runs in runner._table([{**row, "harness": runs}]).splitlines()[1]
    if runs == "pi":
        with pytest.raises(ValueError, match="not claude-sdk"):
            runner.target(engine, None, "claude-sdk")  # type: ignore[arg-type]
