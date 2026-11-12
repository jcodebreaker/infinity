"""Tasks: what a turn reads in front of the person's message, from the Round record (ADR 0029).

Nothing writes a task from a Helper's start or finish any more: the work still under way is the
Conversation's Rounds whose Helpers still run, and a card's pick is the answer the Round's
message carried.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.context.labels import ROUND_LABEL_KEY, SCHEDULED_HELPER_LABEL_KEY
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.cards.handlers import handle_clarification_tool
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.rounds.store import RoundEvent, SqlAlchemyRoundStore
from omnigent.superchat.tasks import runtime
from omnigent.superchat.tasks.record import (
    MAX_LINES,
    CardAnswer,
    Turn,
    ends_well,
    kind_for_name,
    parse_card_answer,
    prefix_lines,
    round_line,
)
from omnigent.superchat.tasks.routes import create_tasks_router
from omnigent.superchat.tasks.service import TaskService
from omnigent.superchat.tasks.store import SqlAlchemyTaskStore

_MODE = {"omnigent.context.mode": "superside-chat"}

# ---------------------------------------------------------------- the lines (pure)


def _working(ask: str = "Make a deck on Uruguay", *titles: str | None) -> dict[str, Any]:
    helpers = [{"id": f"h{n}", "title": t} for n, t in enumerate(titles or ("Uruguay deck",))]
    return {"round_id": "r1", "ask": ask, "helpers": helpers}


def test_a_working_round_reads_as_one_line_with_what_comes_next() -> None:
    assert round_line(_working()) == (
        'Open task: "Make a deck on Uruguay" · helper "Uruguay deck" · next: wait for the helper'
    )
    two = round_line(_working("Two decks,\n  please", "Deck A", None))
    assert (
        two
        == 'Open task: "Two decks, please" · helper "Deck A" · helper · next: wait for the helper'
    )
    long = round_line(_working("word " * 40))
    assert long.startswith('Open task: "word word') and '…" · helper' in long
    # With no Helper running there is nothing to wait for; a finished one says how it ended.
    assert round_line({"helpers": []}) == "Open task"
    done = {"round_id": "r1", "ask": "A deck", "helpers": [{"title": "Deck", "status": "done"}]}
    assert round_line(done) == 'Open task: "A deck" · helper "Deck" done'


def test_the_pick_comes_first_and_the_lines_are_capped() -> None:
    rounds = [_working(f"Deck {n}") for n in range(8)]
    lines = prefix_lines(rounds, CardAnswer("c1", "pitch-deck", "Pitch Deck"))
    assert lines[0] == "They picked Pitch Deck. Continue the task; do not plan it again."
    assert len(lines) == MAX_LINES and lines[1].startswith('Open task: "Deck 0"')
    assert prefix_lines([], None) == []


def test_a_card_answer_is_read_only_in_its_shape() -> None:
    answer = {"card_id": "c1", "option_id": "nord", "label": "  Nord  "}
    assert parse_card_answer({"card_answer": answer}) == CardAnswer("c1", "nord", "Nord")
    assert parse_card_answer({"card_answer": {**answer, "label": ""}}) is None
    assert parse_card_answer({"card_answer": {"card_id": "c1", "label": "x"}}) is None
    assert parse_card_answer({"open_artifact": {}}) is None
    assert parse_card_answer(None) is None


def test_a_saved_files_kind_is_read_from_its_name() -> None:
    assert [kind_for_name(n) for n in ("a.deck.html", "a.html", "a.md", "a.csv", "a.png")] == [
        "deck",
        "page",
        "document",
        "sheet",
        "file",
    ]


@pytest.mark.parametrize(
    ("turn", "well"),
    [
        (Turn(by_person=True, task_id="t"), False),
        (Turn(by_person=True, task_id="t", saved=True), True),
        (Turn(by_person=True, task_id="t", asked=True), True),
        (Turn(by_person=True, task_id="t", helper_started=True), True),
        (Turn(by_person=True), True),
        (Turn(by_person=False, task_id="t"), True),  # a notice or a Helper's wake
        (Turn(by_person=True, task_id="t", helper_running=True), True),  # a steer
    ],
)
def test_the_one_stall_rule(turn: Turn, well: bool) -> None:
    assert ends_well(turn) is well


# ---------------------------------------------------------------- the service and routes


class _LocalAuth:
    def get_user_id(self, request: Request) -> str | None:
        return None


def _say(conversations: SqlAlchemyConversationStore, chat: str, text: str, **flags: Any) -> str:
    data = MessageData(role="user", content=[{"type": "input_text", "text": text}], **flags)
    (item,) = conversations.append(
        chat, [NewConversationItem(type="message", response_id="resp_1", data=data)]
    )
    return item.id


@dataclasses.dataclass
class _Env:
    client: TestClient
    service: TaskService
    conversations: SqlAlchemyConversationStore
    rounds: SqlAlchemyRoundStore
    chat: str


@pytest.fixture()
def env(db_uri: str) -> _Env:
    """The task service behind its routes, and a Superchat Conversation."""
    conversations = SqlAlchemyConversationStore(db_uri)
    rounds = SqlAlchemyRoundStore(db_uri)
    service = TaskService(SqlAlchemyTaskStore(db_uri), conversations, rounds)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _h(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.http_status)

    app.include_router(create_tasks_router(service, auth_provider=_LocalAuth()), prefix="/v1")
    chat = conversations.create_conversation(labels=_MODE).id
    return _Env(TestClient(app), service, conversations, rounds, chat)


def _view(env: _Env, session: str | None = None) -> dict[str, Any]:
    return env.client.get(f"/v1/sessions/{session or env.chat}/tasks").json()


def _open_round(env: _Env, text: str) -> str:
    """The person's message *text*, opening a Round: ``round.opened`` and the session's label,
    as ``rounds.record.note_message`` writes them."""
    rid = _say(env.conversations, env.chat, text)
    env.conversations.set_labels(env.chat, {ROUND_LABEL_KEY: rid})
    env.rounds.append([RoundEvent(rid, env.chat, "round.opened", "person", 1, {"text": text})])
    return rid


def _start_helper(env: _Env, rid: str, title: str, **labels: str) -> str:
    """A running Helper started in Round *rid* (``helper.started``, its Round label)."""
    helper = env.conversations.create_conversation(
        kind="sub_agent",
        parent_conversation_id=env.chat,
        labels={**_MODE, ROUND_LABEL_KEY: rid, **labels},
    ).id
    env.conversations.set_session_live_status(helper, "running")
    env.rounds.append(
        [
            RoundEvent(
                rid,
                env.chat,
                "helper.started",
                "assistant",
                2,
                {"helper_id": helper, "title": title},
            )
        ]
    )
    return helper


def _turn_ended(env: _Env, rid: str) -> None:
    env.rounds.append(
        [RoundEvent(rid, env.chat, "turn.ended", "assistant", 3, {"status": "done"})]
    )


def test_a_round_not_settled_is_open_work_until_its_view_settles(env: _Env) -> None:
    """The Round view is the only source: a Round reads as open from the asking Round's turns
    while its view is not settled, with its Helpers as the view has them, whatever their
    sessions say, and stops the moment it settles. The asking Round is never its own open work,
    and a Round whose last turn failed is settled."""
    rid = _open_round(env, "a deck about our 2026 sales")
    helper = _start_helper(env, rid, "Sales deck")
    _turn_ended(env, rid)
    assert _view(env)["rounds"] == []  # the Round the turn is in
    view = _view(env, helper)
    assert (view["conversation_id"], view["is_helper"], view["round_id"]) == (env.chat, True, rid)
    assert view["message"] == "a deck about our 2026 sales" and view["by_person"] is False
    assert view["background"] is False
    # the next person's message opens its own Round: the Helper's still reads as open
    later = _open_round(env, "and a memo")
    working = {"round_id": rid, "ask": "a deck about our 2026 sales"}
    running = {"id": helper, "title": "Sales deck", "status": "running"}
    assert _view(env)["rounds"] == [{**working, "helpers": [running]}]
    assert _view(env)["round_id"] == later
    # the Helper's session ends before its finish is recorded: the view still runs it
    env.conversations.set_session_live_status(helper, "idle")
    assert _view(env)["rounds"] == [{**working, "helpers": [running]}]
    # its finish waits for the wake turn that reads it; then the Round settles
    env.rounds.append(
        [RoundEvent(rid, env.chat, "helper.finished", "helper", 4, {"helper_id": helper})]
    )
    assert _view(env)["rounds"] == [{**working, "helpers": [{**running, "status": "done"}]}]
    env.rounds.append([RoundEvent(rid, env.chat, "turn.started", "assistant", 5, {})])
    _turn_ended(env, rid)
    assert _view(env)["rounds"] == []
    # a Round whose last turn failed is settled; a scheduled Helper is background work
    _turn_ended(env, later)
    failed = _open_round(env, "a page")
    end = {"status": "failed", "reason": "relay lost"}
    env.rounds.append([RoundEvent(failed, env.chat, "turn.ended", "assistant", 6, end)])
    _open_round(env, "anything else")
    assert _view(env)["rounds"] == []
    scheduled = _start_helper(env, later, "Report", **{SCHEDULED_HELPER_LABEL_KEY: "t1"})
    assert _view(env, scheduled)["background"] is True


def test_no_writer_is_left_and_an_unknown_session_reads_nothing(env: _Env) -> None:
    for event in ("deck_new", "artifact_save", "start_helper", "turn_end", "check", "plan"):
        resp = env.client.post(f"/v1/sessions/{env.chat}/tasks/events", json={"event": event})
        assert resp.status_code == 404, event
    assert env.client.get(f"/v1/sessions/{'f' * 32}/tasks").status_code == 404


def _answer_message(env: _Env, answer: dict[str, str]):
    """The person's pick, sent as the stored message ``POST .../events`` makes (its Round and
    its ``context``)."""
    from omnigent.server.routes._sessions import orchestration

    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": answer["label"]}]},
        context={"card_answer": answer},
    )
    conv = env.conversations.get_conversation(env.chat)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(orchestration, "ON_MESSAGE_CONTEXT", {"tasks": env.service.card_answered})
        return asyncio.run(
            orchestration._persist_user_event_item(env.chat, conv, body, env.conversations)
        )


def test_a_card_answer_is_kept_on_the_message_that_opens_its_round(env: _Env) -> None:
    answer = {"card_id": "ask-1", "option_id": "nord", "label": "Nord"}
    item = _answer_message(env, answer)
    view = _view(env)
    assert view["round_id"] == item.id and view["answer"] == answer and view["by_person"]
    # a Helper the Round started reads the Round's pick (deck_new's look)
    helper = _start_helper(env, item.id, "Deck")
    assert _view(env, helper)["answer"] == answer
    # and a wake turn of the Round still has it, for the look, not as the person's word
    _say(env.conversations, env.chat, "[System: worker/Deck saved a draft]", is_system_notice=True)
    assert (_view(env)["answer"], _view(env)["by_person"]) == (answer, False)
    # the answer lives with the message: a new service (a restart) still reads it
    fresh = TaskService(env.service.store, env.conversations, env.rounds)
    assert fresh.view(env.chat)["answer"] == answer
    # the next Round is another message: the pick is history
    _open_round(env, "and make it short")
    assert _view(env)["answer"] is None


def test_deleting_the_conversation_drops_its_answers(env: _Env) -> None:
    item = _answer_message(env, {"card_id": "c", "option_id": "o", "label": "L"})
    env.service.conversation_deleted(env.chat)
    assert env.service.store.answer(env.chat, item.id) is None


# ---------------------------------------------------------------- the runner side


def _wire(
    rounds: list[dict[str, Any]],
    answer: dict[str, str] | None = None,
    *,
    at: str = "conv1",
    by_person: bool = True,
    round_id: str | None = "r9",
) -> dict[str, Any]:
    return {
        "conversation_id": at,
        "rounds": rounds,
        "answer": answer,
        "by_person": by_person,
        "round_id": round_id,
    }


def _server(view: dict[str, Any] | None):
    def handler(request: httpx.Request) -> httpx.Response:
        if view is not None and request.url.path.endswith("/tasks"):
            return httpx.Response(200, json=view)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://srv")


@pytest.fixture(autouse=True)
def _fresh_runtime(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_turns", {})


async def test_the_turn_gets_the_pick_then_one_line_per_working_round() -> None:
    answer = {"card_id": "c1", "option_id": "pitch-deck", "label": "Pitch Deck"}
    view = _wire([_working()], answer)
    blocks = await runtime.message_prefix(_server(view), "conv1", ["Pitch Deck"])
    assert blocks == [
        "They picked Pitch Deck. Continue the task; do not plan it again.\n"
        'Open task: "Make a deck on Uruguay" · helper "Uruguay deck" · next: wait for the helper'
    ]
    # a wake turn of the pick's Round is not the person's pick again
    wake = _wire([], answer, by_person=False)
    assert await runtime.message_prefix(_server(wake), "conv1", ["[System: x finished]"]) == []
    assert await runtime.message_prefix(_server(_wire([])), "conv1", ["hi"]) == []
    # a view of another conversation (a Helper's root) is not this turn's
    other = _wire([_working()], at="root")
    assert await runtime.message_prefix(_server(other), "conv1", ["go"]) == []
    assert await runtime.message_prefix(_server(None), "conv1", ["hi"]) == []
    assert await runtime.message_prefix(None, "conv1", ["hi"]) == []


async def test_a_card_names_the_round_the_turn_is_in() -> None:
    args = {"question": "Which data?", "options": ["Mock", "Real"]}
    ctx = HandlerCtx("ask_clarification", None, "conv7")
    first = json.loads(await handle_clarification_tool(ctx, args))
    assert "round_id" not in first and first["id"].startswith("ask-")  # the engine's card id
    assert json.loads(await handle_clarification_tool(ctx, args))["id"] != first["id"]
    await runtime.message_prefix(_server(_wire([], at="conv7", round_id="r7")), "conv7", ["go"])
    card = json.loads(await handle_clarification_tool(ctx, args))
    assert card["round_id"] == "r7" and "task_id" not in card
    runtime.turn_ended("conv7")
    assert "round_id" not in json.loads(await handle_clarification_tool(ctx, args))


# ---------------------------------------------------------------- bounded calls (ADR 0026)


async def test_a_slow_server_never_holds_the_read_and_no_result_is_posted(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "BUDGET", 0.05)
    posted: list[str] = []

    async def hang(request: httpx.Request) -> httpx.Response:
        posted.append(request.method)
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(hang), base_url="http://srv")
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await runtime.read_tasks(client, "conv5") is None
    await asyncio.sleep(0)
    assert loop.time() - start < 1
    # the Round record has the check from the relay: the runner posts nothing
    assert posted == ["GET"]


def test_the_read_has_a_two_second_budget() -> None:
    assert runtime._TIMEOUT.read == 2.0 and runtime._TIMEOUT.connect == 1.0


# ---------------------------------------------------------------- the prefix seam


@pytest.mark.parametrize(
    ("kwargs", "read", "profile"),
    [({"helper": True}, False, False), ({"studio": True}, False, True), ({}, True, True)],
    ids=["helper", "studio", "conversation"],
)
async def test_only_the_conversations_turn_reads_the_view(
    monkeypatch, kwargs, read, profile
) -> None:
    """A Helper's turn also skips the memory profile: its brief carries what applies."""
    from omnigent.superchat import features, prompt_prefix

    calls: list[str] = []
    profiles: list[str] = []

    async def prefix(_client, conversation_id: str, _texts) -> list[str]:
        calls.append(conversation_id)
        return []

    async def fetch_profile(_client, conversation_id: str) -> None:
        profiles.append(conversation_id)

    swapped = [
        dataclasses.replace(f, message_prefix=prefix) if f.name == "tasks" else f
        for f in features.FEATURES
    ]
    assert next(f for f in swapped if f.name == "tasks").conversation_only
    monkeypatch.setattr(features, "FEATURES", swapped)
    monkeypatch.setattr(prompt_prefix, "fetch_memory_profile", fetch_profile)
    await prompt_prefix.turn_prefix_blocks(None, "s1", message_texts=["go"], **kwargs)
    assert calls == (["s1"] if read else [])
    assert profiles == (["s1"] if profile else [])


async def test_the_message_prefix_features_are_fetched_together(monkeypatch) -> None:
    from omnigent.superchat import features, prompt_prefix

    started: list[str] = []
    gate = asyncio.Event()

    def slow(name: str):
        async def prefix(_client, _conversation_id: str, _texts) -> list[str]:
            started.append(name)
            if len(started) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1)  # each waits for the other: only together
            return [name]

        return prefix

    swapped = [
        dataclasses.replace(f, message_prefix=slow(f.name), conversation_only=False)
        for f in features.FEATURES[:2]
    ]
    monkeypatch.setattr(features, "FEATURES", swapped)
    blocks = await prompt_prefix._message_prefix_blocks(None, "s1", ["go"])
    assert blocks == [f.name for f in swapped]


def test_a_turns_end_reaches_every_listening_feature(monkeypatch) -> None:
    from omnigent.superchat import features

    ended: list[str] = []

    def boom(_conversation_id: str) -> None:
        raise RuntimeError("listener failed")

    swapped = [
        dataclasses.replace(features.FEATURES[0], on_turn_end=boom),
        dataclasses.replace(features.FEATURES[1], on_turn_end=ended.append),
    ]
    monkeypatch.setattr(features, "FEATURES", swapped)
    features.notify_turn_end("c1")
    assert ended == ["c1"]


# ---------------------------------------------------- the runner and the server together


async def test_the_runner_reads_a_working_round_until_its_helper_ends(env: _Env) -> None:
    """Live, through the real routes: the person's pick starts a Helper; the next message's
    prefix reads that Round as working while its view runs the Helper, and nothing once the
    Round has settled."""
    engine = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.client.app), base_url="http://srv"
    )
    rid = _open_round(env, "a new Acme vs Globex deck")
    helper = _start_helper(env, rid, "Acme vs Globex deck")
    _turn_ended(env, rid)
    later = _open_round(env, "what about the colours?")
    blocks = await runtime.message_prefix(engine, env.chat, ["what about the colours?"])
    assert blocks == [
        'Open task: "a new Acme vs Globex deck" · helper "Acme vs Globex deck" · next: wait for the helper'
    ]
    assert runtime.current_round(env.chat) == later
    runtime.turn_ended(env.chat)
    env.rounds.append(
        [RoundEvent(rid, env.chat, "helper.finished", "helper", 4, {"helper_id": helper})]
    )
    env.rounds.append([RoundEvent(rid, env.chat, "turn.started", "assistant", 5, {})])
    _turn_ended(env, rid)
    notice = "[System: worker/Acme vs Globex deck finished]"
    _say(env.conversations, env.chat, notice, is_system_notice=True)
    assert await runtime.message_prefix(engine, env.chat, [notice]) == []
