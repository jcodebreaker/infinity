"""A file's studio chat (ADR 0024): its group id, its session, its saves and what it is told."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.context.labels import (
    ARTIFACT_CHAT_LABEL_KEY,
    CONTEXT_MODE_LABEL,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.activity.derive import list_activities
from omnigent.superchat.artifacts import links
from omnigent.superchat.artifacts import routes as artifact_routes
from omnigent.superchat.artifacts import store as store_module
from omnigent.superchat.artifacts import studio as studio_module
from omnigent.superchat.artifacts.routes import create_artifacts_router
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
from omnigent.superchat.artifacts.studio import (
    STUDIO_TYPE,
    StudioNotices,
    deliver_studio_notice,
    find_or_create_studio,
    notice_line,
    studio_session_id,
)
from omnigent.superchat.artifacts.studio_turn import _NOTED, studio_turn_blocks
from omnigent.superchat.subagents import count_live_children

CHAT_LABELS = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}


class _LocalAuth:
    def get_user_id(self, request: Request) -> str | None:
        return RESERVED_USER_LOCAL


@pytest.fixture()
def env(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def _allow(*_args: Any, **_kwargs: Any) -> Any:
        return type("Access", (), {"conversation": None})()

    monkeypatch.setattr(artifact_routes, "require_access", _allow)
    linked: list[tuple[str, str]] = []
    monkeypatch.setattr(links, "notify_item_updated", lambda c, i: linked.append((c, i)))
    # The stored notice is published to the session's live stream; that is not under test.
    monkeypatch.setattr(studio_module, "append_notice", _append_without_publish)
    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    store = SqlAlchemyArtifactStore(db_uri, lambda: blobs)
    conversations = SqlAlchemyConversationStore(db_uri)
    notices = StudioNotices(conversations, None)
    monkeypatch.setitem(store_module.ON_GROUP_DELETED, "studio", notices.on_deleted)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _h(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.http_status
        )

    app.include_router(
        create_artifacts_router(
            store,
            conversation_store=conversations,
            auth_provider=_LocalAuth(),  # type: ignore[arg-type]
            permission_store=object(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    chat = conversations.create_conversation(labels=dict(CHAT_LABELS)).id
    return TestClient(app), app, store, conversations, chat, linked


def _append_without_publish(conversations: Any, session_id: str, text: str) -> None:
    conversations.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id=f"studio_{uuid.uuid4().hex}",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": text}],
                    is_system_notice=True,
                ),
            )
        ],
    )


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


def _save(client: TestClient, chat: str, name: str, body: bytes = b"<p>x</p>", **query: Any):
    extra = "".join(f"&{k}={v}" for k, v in query.items())
    return client.post(f"/v1/artifacts?parent_session_id={chat}&name={name}{extra}", content=body)


def _texts(conversations: SqlAlchemyConversationStore, session_id: str) -> list[str]:
    page = conversations.list_items(session_id, limit=50, order="asc")
    return [
        block["text"]
        for item in page.data
        if isinstance(item.data, MessageData)
        for block in item.data.content
        if isinstance(block.get("text"), str)
    ]


# ---------------------------------------------------------------- the group id


def test_every_version_carries_its_groups_id_and_backfill_names_version_one(env, db_uri) -> None:
    client, _app, store, _conversations, chat, _linked = env
    first = _save(client, chat, "deck.md").json()
    second = _save(client, chat, "deck.md", body=b"<p>y</p>").json()
    other = _save(client, chat, "memo.md").json()
    assert first["group_id"] == second["group_id"] == first["id"]
    assert other["group_id"] == other["id"] != first["group_id"]
    assert store.newest_in_group(first["id"], user_id=None).id == second["id"]

    from omnigent.db.migrations.versions.sg1b2c3d4e5f_artifact_group_id import BACKFILL

    engine = get_or_create_engine(db_uri)
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE artifacts SET group_id = NULL"))
        connection.execute(sa.text(BACKFILL))
    detail = client.get(f"/v1/artifacts/{second['id']}").json()
    assert detail["group_id"] == first["id"]
    # A version saved after the backfill keeps the group's id.
    assert _save(client, chat, "deck.md", body=b"<p>z</p>").json()["group_id"] == first["id"]


# ---------------------------------------------------------------- find-or-create


def test_find_or_create_makes_one_quiet_session_per_file(env) -> None:
    client, _app, _store, conversations, chat, _linked = env
    v1 = _save(client, chat, "deck.md").json()
    v2 = _save(client, chat, "deck.md", body=b"<p>y</p>").json()
    made = client.post(f"/v1/artifacts/{v1['id']}/chat")
    assert made.status_code == 200, made.text
    session_id = made.json()["session_id"]
    assert made.json()["created"] is True
    # Any version, any number of times: the same session.
    again = client.post(f"/v1/artifacts/{v2['id']}/chat").json()
    assert again == {"session_id": session_id, "created": False}
    assert session_id == studio_session_id(None, chat, v1["id"])
    studio = conversations.get_conversation(session_id)
    assert studio is not None
    assert studio.kind == "sub_agent" and studio.sub_agent_name == STUDIO_TYPE
    assert studio.parent_conversation_id == chat
    assert studio.labels[ARTIFACT_CHAT_LABEL_KEY] == v1["id"]
    # No wake: nothing was dispatched, nothing posted to the studio or the Conversation.
    assert studio.live_status is None
    assert conversations.list_items(session_id, limit=10).data == []
    assert conversations.list_items(chat, limit=10).data == []


def test_another_owners_file_and_a_deleted_file_are_not_found(env) -> None:
    client, _app, store, conversations, chat, _linked = env
    theirs = store.create(
        user_id="alice",
        parent_session_id=chat,
        name="theirs.md",
        title=None,
        kind="md",
        mime="text/markdown",
        data=b"# x",
    )
    assert client.post(f"/v1/artifacts/{theirs.id}/chat").status_code == 404
    mine = _save(client, chat, "mine.md").json()
    session_id = client.post(f"/v1/artifacts/{mine['id']}/chat").json()["session_id"]
    assert client.delete(f"/v1/artifacts/{mine['id']}").status_code == 200
    assert client.post(f"/v1/artifacts/{mine['id']}/chat").status_code == 404
    studio = conversations.get_conversation(session_id)
    assert studio is not None and studio.archived


def test_a_racing_create_is_caught_and_read_back(env) -> None:
    client, _app, store, conversations, chat, _linked = env
    saved = _save(client, chat, "deck.md").json()
    item = store.get(saved["id"], user_id=None)
    assert item is not None
    session_id, created = find_or_create_studio(conversations, item)
    assert created

    class _Late:
        """A store whose first read misses the row a concurrent open just wrote."""

        def __init__(self) -> None:
            self.reads = 0

        def get_conversation(self, conversation_id: str) -> Any:
            self.reads += 1
            if self.reads == 1:
                return None
            return conversations.get_conversation(conversation_id)

        def __getattr__(self, name: str) -> Any:
            return getattr(conversations, name)

    assert find_or_create_studio(_Late(), item) == (session_id, False)  # type: ignore[arg-type]


# ---------------------------------------------------------------- isolation


def test_the_studio_is_outside_listings_caps_and_activity(env) -> None:
    client, _app, _store, conversations, chat, _linked = env
    saved = _save(client, chat, "deck.md").json()
    session_id = client.post(f"/v1/artifacts/{saved['id']}/chat").json()["session_id"]
    _ask(conversations, session_id, "make the title bigger")
    studio = conversations.get_conversation(session_id)
    assert studio is not None
    conversations.set_session_live_status(session_id, "running")
    running = conversations.get_conversation(session_id)
    row = {"tool": STUDIO_TYPE, "labels": running.labels, "busy": True}  # type: ignore[union-attr]
    assert count_live_children([row]) == 0
    assert count_live_children([{**row, "tool": "worker", "labels": {}}]) == 1
    # Not a Helper row, not work in the strip.
    assert all(a.chat_id != session_id for a in list_activities(conversations, chat))


# ---------------------------------------------------------------- saves from the studio


def test_a_studio_save_needs_its_base_and_stays_out_of_super_chat(env) -> None:
    client, _app, _store, conversations, chat, linked = env
    _ask(conversations, chat, "make a deck")
    v1 = _save(client, chat, "deck.md").json()
    linked.clear()
    studio = client.post(f"/v1/artifacts/{v1['id']}/chat").json()["session_id"]
    asked = _ask(conversations, studio, "bigger title")

    missing = _save(client, chat, "deck.md", chat_id=studio)
    assert missing.status_code == 400
    stale = _save(client, chat, "deck.md", chat_id=studio, base_version=5)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "artifact_stale"
    assert "v1" in stale.json()["error"]["message"]

    ok = _save(client, chat, "deck.md", body=b"<p>y</p>", chat_id=studio, base_version=1)
    assert ok.status_code == 201, ok.text
    version = client.get(f"/v1/artifacts/{ok.json()['id']}").json()["all_versions"][0]
    # The studio turn's own message; no chip on any of Super Chat's messages.
    assert version["item_id"] == asked
    assert version["chat_id"] == studio and version["from_studio"] is True
    first = client.get(f"/v1/artifacts/{v1['id']}").json()["all_versions"][-1]
    assert first["from_studio"] is False
    assert linked == []
    # Another file saved from the studio (an export) needs no base.
    assert _save(client, chat, "deck.pdf", body=b"%PDF", chat_id=studio).status_code == 201


def test_the_stale_tool_result_puts_the_newest_version_in_the_workspace(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.superchat.artifacts import handlers

    _client, app, _store, conversations, chat, _linked = env
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    deck = workspace / "deck.md"
    deck.write_text("# v1")

    async def run() -> tuple[str, str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
            first = await handlers._save(client, chat, {"path": str(deck)}, None)
            group = __import__("json").loads(first)["group_id"]
            studio = studio_session_id(None, chat, group)
            await client.post(f"/v1/artifacts/{group}/chat")
            _ask(conversations, studio, "bigger")
            deck.write_text("# v2 from elsewhere")
            await handlers._save(client, chat, {"path": str(deck)}, None)
            deck.write_text("# my edit on v1")
            refused = await handlers._save(
                client, chat, {"path": str(deck), "base_version": 1}, studio
            )
            return refused, deck.read_text()

    refused, on_disk = asyncio.run(run())
    payload = __import__("json").loads(refused)
    assert payload["error"] == "artifact_stale" and payload["newest_version"] == 2
    assert on_disk == "# v2 from elsewhere"


# ---------------------------------------------------------------- what the studio is told


def _studio_with_edit(client: TestClient, conversations: Any, chat: str):
    v1 = _save(client, chat, "deck.md").json()
    studio = client.post(f"/v1/artifacts/{v1['id']}/chat").json()["session_id"]
    head = client.get(f"/v1/artifacts/{v1['id']}").json()
    return v1, studio, head


def test_a_hand_edit_is_stored_in_the_studio_without_a_turn(env) -> None:
    client, _app, store, conversations, chat, _linked = env
    v1, studio, _head = _studio_with_edit(client, conversations, chat)
    item = store.get(v1["id"], user_id=None)
    assert item is not None
    edit = store.create(
        user_id=None,
        parent_session_id=chat,
        name="deck.md",
        title=None,
        kind="md",
        mime="text/markdown",
        data=b"# bigger",
        origin="manual",
        edit_summary="made the title larger",
    )
    steered: list[tuple[str, str]] = []

    async def steer(session_id: str, text: str) -> bool:
        steered.append((session_id, text))
        return True

    assert asyncio.run(deliver_studio_notice(conversations, edit, steer=steer)) == "stored"
    assert steered == []
    assert _texts(conversations, studio) == ["[you] made the title larger · v2"]
    studio_row = conversations.get_conversation(studio)
    assert studio_row is not None and studio_row.live_status is None
    # Super Chat's own save is recorded with its origin; the studio's own save is not.
    from_chat = store.create(
        user_id=None,
        parent_session_id=chat,
        name="deck.md",
        title=None,
        kind="md",
        mime="text/markdown",
        data=b"# three",
        chat_id=chat,
    )
    assert notice_line(from_chat, studio) == "[Super Chat] saved v3"
    own = store.create(
        user_id=None,
        parent_session_id=chat,
        name="deck.md",
        title=None,
        kind="md",
        mime="text/markdown",
        data=b"# four",
        chat_id=studio,
    )
    assert asyncio.run(deliver_studio_notice(conversations, own, steer=steer)) is None


def test_a_running_studio_turn_is_steered_with_the_same_line(env) -> None:
    client, _app, store, conversations, chat, _linked = env
    _v1, studio, _head = _studio_with_edit(client, conversations, chat)
    conversations.set_session_live_status(studio, "running")
    restored = store.create(
        user_id=None,
        parent_session_id=chat,
        name="deck.md",
        title=None,
        kind="md",
        mime="text/markdown",
        data=b"# x",
        origin="restore",
        edit_summary="restored version 1",
    )
    steered: list[tuple[str, str]] = []

    async def steer(session_id: str, text: str) -> bool:
        steered.append((session_id, text))
        return True

    assert asyncio.run(deliver_studio_notice(conversations, restored, steer=steer)) == "steered"
    assert steered == [(studio, "[you] restored version 1 · v2")]
    assert _texts(conversations, studio) == []

    async def gone(_session_id: str, _text: str) -> bool:
        return False  # the turn ended meanwhile: the line is kept for the next one

    assert asyncio.run(deliver_studio_notice(conversations, restored, steer=gone)) == "stored"


def test_the_studio_turn_reads_its_file_and_writes_back_only_its_own(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, app, store, conversations, chat, _linked = env
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    deck, memo = workspace / "deck.md", workspace / "memo.md"

    def make(path: Path, origin: str = "ai", summary: str | None = None, body: bytes = b"# a"):
        return store.create(
            user_id=None,
            parent_session_id=chat,
            name=path.name,
            title=None,
            kind="md",
            mime="text/markdown",
            data=body,
            origin=origin,
            source_path=str(path),
            edit_summary=summary,
        )

    first = make(deck)
    make(memo)
    make(deck, "manual", "made the title larger", b"# bigger")
    make(memo, "manual", "fixed a typo", b"# memo fixed")
    studio, _ = find_or_create_studio(conversations, first)
    _NOTED.pop(studio, None)

    async def run() -> tuple[Any, Any, dict[str, Any]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
            blocks = await studio_turn_blocks(client, studio)
            again = await studio_turn_blocks(client, studio)
            pending = (
                await client.get(f"/v1/artifacts/pending-edits?parent_session_id={chat}")
            ).json()
            return blocks, again, pending

    blocks, again, pending = asyncio.run(run())
    (line,), (note,) = blocks
    assert line.startswith("The file of this chat: deck.md (deck.md, document, group ")
    assert "Newest: v2, saved by you" in line and "base_version 2" in line
    assert note.startswith("The person edited deck.md by hand (v1 → v2): made the title larger")
    assert deck.read_bytes() == b"# bigger"
    assert not memo.exists()  # another file's edit is the Conversation's to deliver
    assert again == ([line], [])  # told once
    # Not acknowledged: the Conversation's next turn still gets both notes.
    assert {e["name"] for e in pending["edits"]} == {"deck.md", "memo.md"}
