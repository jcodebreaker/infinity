"""Family reads: POST .../read, search across a Conversation, and its Rounds (with the shadow
diff against today's derivation)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from omnigent.entities import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.errors import OmnigentError
from omnigent.server.routes._sessions.helpers import _prune_session_read_state
from omnigent.server.routes.sessions.routes_items import register_items_routes
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.rounds.read import shadow_diff
from omnigent.superchat.rounds.routes import register_round_routes
from omnigent.superchat.rounds.store import RoundEvent, SqlAlchemyRoundStore

_MODE = {"omnigent.context.mode": "superside-chat"}


def _say(role: str, text: str, response_id: str = "r1") -> NewConversationItem:
    content = [{"type": "input_text" if role == "user" else "output_text", "text": text}]
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role=role, content=content, agent="brain" if role == "assistant" else None
        ),
    )


@pytest.fixture()
def round_store(db_uri: str) -> SqlAlchemyRoundStore:
    return SqlAlchemyRoundStore(db_uri)


@pytest.fixture()
async def client(
    conversation_store: SqlAlchemyConversationStore, round_store: SqlAlchemyRoundStore
) -> httpx.AsyncClient:
    app = FastAPI()
    router = APIRouter()
    register_items_routes(
        router,
        conversation_store=conversation_store,
        agent_store=None,  # type: ignore[arg-type]
    )
    register_round_routes(router, store=round_store, conversation_store=conversation_store)
    app.include_router(router, prefix="/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        yield http


def _root(store: SqlAlchemyConversationStore) -> str:
    return store.create_conversation(kind="default", title="S", labels=_MODE).id


async def test_read_moves_the_baseline_forward_only(
    client: httpx.AsyncClient, conversation_store: SqlAlchemyConversationStore
) -> None:
    root_id = _root(conversation_store)
    try:
        conversation_store.append(root_id, [_say("user", "hi"), _say("assistant", "hello")])
        read = (await client.post(f"/v1/sessions/{root_id}/read")).json()
        assert read["unread"] is False and read["session_id"] == root_id

        # Reading up to an older item never moves the baseline back.
        first = conversation_store.list_items(root_id, limit=1).data[0]
        again = await client.post(f"/v1/sessions/{root_id}/read", json={"item_id": first.id})
        assert again.json()["last_read_at"] == read["last_read_at"]
    finally:
        _prune_session_read_state(root_id)


async def test_read_rejects_an_unknown_item_and_session(
    client: httpx.AsyncClient, conversation_store: SqlAlchemyConversationStore
) -> None:
    root_id = _root(conversation_store)
    with pytest.raises(OmnigentError, match="Item not found"):
        await client.post(f"/v1/sessions/{root_id}/read", json={"item_id": "msg_nope"})
    with pytest.raises(OmnigentError, match="not found"):
        await client.post("/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/read")


async def test_family_search_covers_the_conversation_skips_hidden_and_helpers(
    client: httpx.AsyncClient, conversation_store: SqlAlchemyConversationStore
) -> None:
    root_id = _root(conversation_store)
    helper = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root_id, title="job", labels=_MODE
    )
    hidden = NewConversationItem(
        type="message",
        response_id="r1",
        data=MessageData(
            role="user", content=[{"type": "input_text", "text": "zebra hidden"}], is_meta=True
        ),
    )
    conversation_store.append(
        root_id, [_say("user", "zebra in the root"), _say("assistant", "zebra reply"), hidden]
    )
    conversation_store.append(helper.id, [_say("assistant", "zebra in helper")])

    body = (
        await client.get(
            f"/v1/sessions/{root_id}/items/search", params={"query": "zebra", "scope": "family"}
        )
    ).json()
    hits = {(hit["session_id"], hit["text"]) for hit in body["data"]}
    assert hits == {(root_id, "zebra in the root"), (root_id, "zebra reply")}
    for hit in body["data"]:
        assert hit["message_id"] == hit["item"]["id"]
        assert hit["role"] in ("user", "assistant")

    # The default scope is unchanged: this session's items only.
    own = (
        await client.get(f"/v1/sessions/{root_id}/items/search", params={"query": "zebra"})
    ).json()
    assert [item["id"] for item in own["data"]] and "session_id" not in own["data"][0]


async def test_a_round_reads_folded_from_any_chat_of_its_conversation_and_shadows_today(
    client: httpx.AsyncClient,
    conversation_store: SqlAlchemyConversationStore,
    round_store: SqlAlchemyRoundStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Round route folds the Conversation's Round for the Conversation and its Helper, lists
    it with its status, refuses another Conversation's Round, and the shadow diff logs only the
    fields the record and today's derivation disagree on: a save the record missed is a file
    more than the Round's view has."""
    root_id = _root(conversation_store)
    other_id = _root(conversation_store)
    helper = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root_id, title="job", labels=_MODE
    )

    def save(call_id: str, name: str) -> list[NewConversationItem]:
        saved = json.dumps({"type": "artifact", "id": f"a_{name}", "name": name, "version": 1})
        call = FunctionCallData(agent="brain", call_id=call_id, name="artifact_save", arguments="")
        return [
            NewConversationItem(type="function_call", response_id="r1", data=call),
            NewConversationItem(
                type="function_call_output",
                response_id="r1",
                data=FunctionCallOutputData(call_id=call_id, output=saved),
            ),
        ]

    asked, *_ = conversation_store.append(
        root_id, [_say("user", "Make a deck."), *save("c1", "deck"), _say("assistant", "Done.")]
    )
    rid, at = asked.id, asked.created_at * 1000

    def event(kind: str, actor: str, ms: int, conversation: str = root_id, **body):
        return RoundEvent(rid, conversation, kind, actor, at + ms, body)

    round_store.append(
        [
            event("round.opened", "person", 0, text="Make a deck."),
            event("turn.started", "assistant", 10),
            event("file.saved", "assistant", 20, name="deck", artifact_id="a_deck", version=1),
            event("turn.ended", "assistant", 30, status="done"),
        ]
    )
    view = (await client.get(f"/v1/sessions/{helper.id}/rounds/{rid}")).json()
    assert view["round_id"] == rid and view["status"] == "done" and view["ask"] == "Make a deck."
    assert [f["name"] for f in view["files"]] == ["deck"] and "after_settle" not in view
    listed = (await client.get(f"/v1/sessions/{root_id}/rounds", params={"limit": 5})).json()
    assert [(r["round_id"], r["status"], r["files"]) for r in listed["data"]] == [(rid, "done", 1)]
    with pytest.raises(OmnigentError, match="Round not found"):
        await client.get(f"/v1/sessions/{other_id}/rounds/{rid}")

    with caplog.at_level(logging.INFO, logger="omnigent.superchat.rounds.read"):
        assert shadow_diff(round_store, conversation_store, root_id, rid) == []
        conversation_store.append(root_id, save("c2", "notes"))  # a save the record missed
        assert shadow_diff(round_store, conversation_store, root_id, rid) == ["files"]
    assert [r.getMessage() for r in caplog.records] == [
        f"round {rid} shadow files: fold=['deck'] today=['deck', 'notes']",
    ]
