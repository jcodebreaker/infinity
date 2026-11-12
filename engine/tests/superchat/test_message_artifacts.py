"""A person's message links to an artifact, and a version to its message (docs/conversation.md)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.context.labels import ROUND_LABEL_KEY, SCHEDULED_HELPER_LABEL_KEY
from omnigent.entities import MessageData, NewConversationItem
from omnigent.entities.conversation import FunctionCallData, FunctionCallOutputData
from omnigent.errors import OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.artifacts import links
from omnigent.superchat.artifacts import routes as artifact_routes
from omnigent.superchat.artifacts.hint import HEADER, LIST_TOOL, MAX_ARTIFACTS, artifact_hint
from omnigent.superchat.artifacts.links import (
    STEER_NONE,
    next_link,
    parse_open_artifact,
    record_open_artifact,
)
from omnigent.superchat.artifacts.routes import create_artifacts_router
from omnigent.superchat.artifacts.store import (
    LINK_CHOSEN,
    LINK_EDITED,
    LINK_OPEN,
    Artifact,
    MessageLink,
    SqlAlchemyArtifactStore,
)
from omnigent.superchat.prompt_prefix import person_words
from omnigent.superchat.transcript import routes as transcript_routes
from omnigent.superchat.transcript.routes import register_transcript_routes


def _artifact(name: str, version: int = 1, *, title: str | None = None, kind: str = "html"):
    return Artifact(
        id=uuid.uuid4().hex,
        user_id=None,
        parent_session_id="s",
        name=name,
        title=title,
        kind=kind,
        mime="text/html",
        size=1,
        version=version,
        blob_key="k",
        published=False,
        created_at=0,
        updated_at=None,
    )


# ---------------------------------------------------------------- the hint (pure)


def test_hint_names_artifacts_newest_first_and_marks_the_open_one() -> None:
    deck = _artifact("q3.deck.html", 4, title="Q3 Lending Review")
    memo = _artifact("provisions.md", 1, title="Provisions memo", kind="md")
    hint = artifact_hint([deck, memo], open_artifact=deck)
    assert hint == "\n".join(
        [
            HEADER,
            f"- Q3 Lending Review · deck · v4 · {deck.id} · last change 1970-01-01"
            " — open beside the chat",
            f"- Provisions memo · document · v1 · {memo.id} · last change 1970-01-01",
        ]
    )


def test_hint_is_skipped_without_artifacts_and_counts_the_rest() -> None:
    assert artifact_hint([]) is None
    many = [_artifact(f"p{i}.html") for i in range(MAX_ARTIFACTS + 3)]
    older_open = _artifact("old.csv", 2, kind="csv")
    hint = artifact_hint(many, open_artifact=older_open)
    assert hint is not None
    lines = hint.splitlines()
    assert len(lines) == MAX_ARTIFACTS + 2 and lines[-1] == f"4 more: {LIST_TOOL}"
    # An open artifact older than the ones listed is still named, first.
    assert lines[1].startswith("- old.csv · sheet · v2 · ") and lines[1].endswith("chat")


def test_the_hint_is_cut_from_the_persons_words() -> None:
    hint = artifact_hint([_artifact("a.html")])
    assert person_words(f"{hint}\n\nMake the title bigger") == "Make the title bigger"


# ---------------------------------------------------------------- link rules (pure)


def test_a_save_names_the_artifact_of_a_message_with_none_or_another_open() -> None:
    saved = _artifact("a.html", 3)
    made = next_link(None, saved)
    assert made == MessageLink(saved.id, "a.html", 3, LINK_EDITED)
    other_open = MessageLink("x" * 32, "b.html", None, LINK_OPEN)
    assert next_link(other_open, saved) == made


def test_a_save_fills_the_version_of_the_same_artifact_and_keeps_another_choice() -> None:
    saved = _artifact("a.html", 5)
    opened = MessageLink("o" * 32, "a.html", None, LINK_OPEN)
    assert next_link(opened, saved) == MessageLink("o" * 32, "a.html", 5, LINK_OPEN)
    chosen = MessageLink("c" * 32, "a.html", None, LINK_CHOSEN)
    assert next_link(chosen, saved) == MessageLink("c" * 32, "a.html", 5, LINK_CHOSEN)
    assert next_link(MessageLink("c" * 32, "b.html", None, LINK_CHOSEN), saved) is None
    assert next_link(MessageLink(None, None, None, LINK_CHOSEN), saved) is None
    assert next_link(MessageLink(saved.id, "b.html", 1, LINK_EDITED), saved) is None


def test_the_send_context_is_read_only_in_its_one_shape() -> None:
    good = uuid.uuid4().hex
    assert parse_open_artifact({"open_artifact": {"artifact_id": good, "version": 4}}) == good
    for bad in (None, {}, {"open_artifact": "x"}, {"open_artifact": {"artifact_id": "nope"}}):
        assert parse_open_artifact(bad) is None  # type: ignore[arg-type]
    event = SessionEventInput.model_validate(
        {
            "type": "message",
            "data": {"role": "user", "content": []},
            "context": {"open_artifact": {"artifact_id": good, "version": 4}},
        }
    )
    assert event.context == {"open_artifact": {"artifact_id": good, "version": 4}}
    assert "context" not in event.model_dump()  # never forwarded to the runner or the model


# ---------------------------------------------------------------- routes


class _LocalAuth:
    def get_user_id(self, request: Request) -> str | None:
        return RESERVED_USER_LOCAL


class _Redactor:
    def deep(self, value: Any) -> Any:
        return value


def _ask(conversations: SqlAlchemyConversationStore, chat: str, text: str) -> str:
    (item,) = conversations.append(
        chat,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
        ],
    )
    return item.id


def _started(conversations: SqlAlchemyConversationStore, chat: str, helper: str) -> None:
    """The ``start_helper`` call in *chat* that started *helper*, with its receipt."""
    receipt = json.dumps({"started": True, "helper_id": helper})
    conversations.append(
        chat,
        [
            NewConversationItem(
                type="function_call",
                response_id="resp_1",
                data=FunctionCallData(
                    agent="a", name="start_helper", arguments="{}", call_id="c1"
                ),
            ),
            NewConversationItem(
                type="function_call_output",
                response_id="resp_1",
                data=FunctionCallOutputData(call_id="c1", output=receipt),
            ),
        ],
    )


@pytest.fixture()
def env(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def _allow(*_args: Any, **_kwargs: Any) -> Any:
        return type("Access", (), {"conversation": None})()

    async def _redactor(*_args: Any, **_kwargs: Any) -> _Redactor:
        return _Redactor()

    monkeypatch.setattr(artifact_routes, "require_access", _allow)
    monkeypatch.setattr(artifact_routes, "session_redactor", _redactor)
    monkeypatch.setattr(transcript_routes, "_require_access_and_level", _allow)
    monkeypatch.setattr(transcript_routes, "session_redactor", _redactor)
    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(links, "notify_item_updated", lambda c, i: updates.append((c, i)))
    monkeypatch.setattr(
        artifact_routes, "notify_item_updated", lambda c, i: updates.append((c, i))
    )
    steers: list[tuple[str, str]] = []

    async def _steer(_request: Request, session_id: str, text: str) -> bool:
        steers.append((session_id, text))
        return True

    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    store = SqlAlchemyArtifactStore(db_uri, lambda: blobs)
    conversations = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _h(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.http_status)

    app.include_router(
        create_artifacts_router(
            store,
            conversation_store=conversations,
            auth_provider=_LocalAuth(),  # type: ignore[arg-type]
            permission_store=object(),  # type: ignore[arg-type]
            steer=_steer,
        ),
        prefix="/v1",
    )
    from fastapi import APIRouter

    router = APIRouter()
    register_transcript_routes(
        router,
        conversation_store=conversations,
        auth_provider=_LocalAuth(),  # type: ignore[arg-type]
        artifact_store=store,
    )
    app.include_router(router, prefix="/v1")
    chat = conversations.create_conversation().id
    return TestClient(app), store, conversations, chat, updates, steers


def _save(client: TestClient, chat: str, name: str, body: bytes = b"<p>x</p>", **query: str):
    extra = "".join(f"&{k}={v}" for k, v in query.items())
    resp = client.post(f"/v1/artifacts?parent_session_id={chat}&name={name}{extra}", content=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _transcript_message(client: TestClient, chat: str, item_id: str) -> dict[str, Any]:
    page = client.get(f"/v1/sessions/{chat}/transcript").json()
    return next(m for m in page["data"] if m["id"] == item_id)


def test_a_save_links_the_message_and_the_version_names_it(env) -> None:
    client, _store, conversations, chat, updates, _steers = env
    asked = _ask(conversations, chat, "make the page")
    saved = _save(client, chat, "page.md")
    detail = client.get(f"/v1/artifacts/{saved['id']}").json()
    assert detail["all_versions"][0]["item_id"] == asked
    assert updates == [(chat, asked)]
    assert _transcript_message(client, chat, asked)["artifact"] == {
        "artifact_id": saved["id"],
        "name": "page.md",
        "version": 1,
        "how": "edited",
    }
    # A second save in the same turn fills the newer version; a hand edit names no message.
    again = _save(client, chat, "page.md", body=b"<p>y</p>")
    link = _transcript_message(client, chat, asked)["artifact"]
    assert (link["artifact_id"], link["version"]) == (again["id"], 2)


def test_a_helper_save_names_the_message_the_helper_came_from(env) -> None:
    """A save names the person's message that opened the saving chat's Round and is stamped with
    that Round: a Helper's save, and the Conversation's own save after a steer. A scheduled
    run, in no Round, names no message."""
    client, store, conversations, chat, _updates, _steers = env
    asked = _ask(conversations, chat, "write the memo")
    in_round = {"omnigent.context.mode": "superside-chat", ROUND_LABEL_KEY: asked}
    conversations.set_labels(chat, in_round)
    helper = conversations.create_conversation(parent_conversation_id=chat, labels=in_round).id
    _started(conversations, chat, helper)
    _ask(conversations, helper, "brief: write the memo")
    steer = _ask(conversations, chat, "and a later message, sent while it works")
    saved = _save(client, chat, "memo.md", chat_id=helper)
    version = client.get(f"/v1/artifacts/{saved['id']}").json()["all_versions"][0]
    assert version["item_id"] == asked
    assert _transcript_message(client, chat, asked)["artifact"]["how"] == "edited"
    own = _save(client, chat, "notes.md")
    version = client.get(f"/v1/artifacts/{own['id']}").json()["all_versions"][0]
    assert version["item_id"] == asked != steer
    assert {store.get_in_session(a["id"], chat).round_id for a in (saved, own)} == {asked}

    scheduled = conversations.create_conversation(
        parent_conversation_id=chat, labels={SCHEDULED_HELPER_LABEL_KEY: "t1"}
    ).id
    _ask(conversations, scheduled, "the weekly run")
    run = _save(client, chat, "weekly.md", chat_id=scheduled)
    assert client.get(f"/v1/artifacts/{run['id']}").json()["all_versions"][0]["item_id"] is None


def test_the_list_names_the_chat_and_origin_of_each_newest_version(env) -> None:
    client, _store, conversations, chat, _updates, _steers = env
    _ask(conversations, chat, "write the memo")
    helper = conversations.create_conversation(parent_conversation_id=chat).id
    _started(conversations, chat, helper)
    _save(client, chat, "page.md")
    _save(client, chat, "memo.md", chat_id=helper)
    rows = {row["name"]: row for row in client.get("/v1/artifacts").json()["artifacts"]}
    assert (rows["memo.md"]["chat_id"], rows["memo.md"]["origin"]) == (helper, "ai")
    assert rows["page.md"]["chat_id"] != helper


def test_the_open_artifact_links_the_message_and_leads_the_hint(env) -> None:
    client, store, conversations, chat, _updates, _steers = env
    _ask(conversations, chat, "make two files")
    deck = _save(client, chat, "q3.deck.html", body=b"<html>deck</html>")
    memo = _save(client, chat, "memo.md")
    asked = _ask(conversations, chat, "make the title bigger")
    context = {"open_artifact": {"artifact_id": deck["id"], "version": 1}}
    assert record_open_artifact(store, chat, asked, context) is not None
    assert _transcript_message(client, chat, asked)["artifact"] == {
        "artifact_id": deck["id"],
        "name": "q3.deck.html",
        "version": None,
        "how": "open",
    }
    hint = client.get(f"/v1/artifacts/hint?session_id={chat}").json()["hint"]
    assert hint.splitlines() == [
        HEADER,
        f"- memo.md (document, v1, {memo['id']})",
        f"- q3.deck.html (deck, v1, {deck['id']}) — open beside the chat",
    ]
    # Another Conversation's artifact is never recorded as open.
    other = conversations.create_conversation().id
    _ask(conversations, other, "hello")
    stray = _ask(conversations, chat, "next")
    foreign = {"open_artifact": {"artifact_id": _save(client, other, "x.md")["id"]}}
    assert record_open_artifact(store, chat, stray, foreign) is None


def test_the_hint_is_none_without_artifacts_and_for_a_helper(env) -> None:
    client, _store, conversations, chat, _updates, _steers = env
    assert client.get(f"/v1/artifacts/hint?session_id={chat}").json() == {"hint": None}
    helper = conversations.create_conversation(parent_conversation_id=chat).id
    _ask(conversations, chat, "go")
    _save(client, chat, "a.md")
    assert client.get(f"/v1/artifacts/hint?session_id={helper}").json() == {"hint": None}


def test_the_persisted_message_runs_the_context_hook(env, monkeypatch) -> None:
    from omnigent.server.routes._sessions import orchestration

    _client, _store, conversations, chat, _updates, _steers = env
    seen: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setitem(
        orchestration.ON_MESSAGE_CONTEXT, "test", lambda s, i, c: seen.append((s, i, c))
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        context={"open_artifact": {"artifact_id": "a" * 32, "version": 1}},
    )
    conv = conversations.get_conversation(chat)
    item = asyncio.run(orchestration._persist_user_event_item(chat, conv, body, conversations))
    assert seen == [(chat, item.id, {"open_artifact": {"artifact_id": "a" * 32, "version": 1}})]


def test_relinking_a_message_answers_with_it_and_steers_only_a_running_turn(env) -> None:
    client, _store, conversations, chat, updates, steers = env
    asked = _ask(conversations, chat, "change the chart")
    _save(client, chat, "first.md")
    picked = _save(client, chat, "second.md")

    url = f"/v1/sessions/{chat}/items/{asked}/artifact"
    resp = client.post(url, json={"artifact_id": picked["id"]})
    assert resp.status_code == 200
    message = resp.json()
    assert message["id"] == asked and message["role"] == "user"
    assert message["blocks"] == [{"type": "text", "text": "change the chart"}]
    assert message["artifact"] == {
        "artifact_id": picked["id"],
        "name": "second.md",
        "version": None,
        "how": "chosen",
    }
    assert updates[-1] == (chat, asked)
    assert steers == []  # the turn is over: the next message carries the link instead

    conversations.set_session_live_status(chat, "running")
    resp = client.post(url, json={"artifact_id": None})
    assert resp.status_code == 200 and resp.json()["artifact"] is None
    assert steers == [(chat, STEER_NONE)]
    client.post(url, json={"artifact_id": picked["id"]})
    assert steers[-1] == (chat, f'The person means the artifact "second.md" ({picked["id"]}).')
    assert _transcript_message(client, chat, asked)["artifact"]["how"] == "chosen"


def test_relinking_what_is_not_the_persons_message_is_not_found(env) -> None:
    client, _store, conversations, chat, _updates, _steers = env
    (reply,) = conversations.append(
        chat,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="assistant", agent="a", content=[{"type": "output_text", "text": "ok"}]
                ),
            )
        ],
    )
    for item_id in (reply.id, uuid.uuid4().hex, "not-an-id"):
        resp = client.post(
            f"/v1/sessions/{chat}/items/{item_id}/artifact", json={"artifact_id": None}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


def test_deleting_an_artifact_drops_the_links_to_it(env) -> None:
    client, _store, conversations, chat, _updates, _steers = env
    asked = _ask(conversations, chat, "make it")
    saved = _save(client, chat, "gone.md")
    assert "artifact" in _transcript_message(client, chat, asked)
    client.delete(f"/v1/artifacts/{saved['id']}")
    assert "artifact" not in _transcript_message(client, chat, asked)


def test_a_code_posted_message_shows_the_file_its_run_made_as_a_card(env) -> None:
    """ADR 0038: the Lookout's (and the brief's) message carries the draft as its file card."""
    client, store, conversations, chat, _updates, _steers = env
    saved = _save(client, chat, "q3-update.html")
    (posted,) = conversations.append(
        chat,
        [
            NewConversationItem(
                type="message",
                response_id="recorded_lookout_w1",
                data=MessageData(
                    role="assistant",
                    agent="builder",
                    content=[{"type": "output_text", "text": "Open the draft?"}],
                ),
            )
        ],
    )
    assert not links.link_made(store, chat, posted.id, uuid.uuid4().hex)  # not this chat's
    assert links.link_made(store, chat, posted.id, saved["id"])
    blocks = _transcript_message(client, chat, posted.id)["blocks"]
    assert blocks[0] == {"type": "text", "text": "Open the draft?"}
    assert blocks[1]["type"] == "file" and blocks[1]["artifact_id"] == saved["id"]
    assert blocks[1]["name"] == "q3-update.html" and blocks[1]["version"] == 1
