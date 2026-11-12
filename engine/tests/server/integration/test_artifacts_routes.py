"""Integration tests for ``/v1/artifacts`` and the ``artifact_*`` runner tools."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from omnigent.entities import MessageData, NewConversationItem
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from omnigent.superchat.approvals.policy import classify_tool_call
from omnigent.superchat.artifacts import handlers
from omnigent.superchat.artifacts.handlers import handle_artifact_tool
from omnigent.superchat.feature import HandlerCtx

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def env(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    from omnigent import runtime

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    runtime._globals._artifact_store = artifact_store
    conversations = SqlAlchemyConversationStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
    )
    chat = conversations.create_conversation().id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        yield client, chat


async def test_versions_content_and_delete(env: tuple[httpx.AsyncClient, str]) -> None:
    client, chat = env
    url = f"/v1/artifacts?parent_session_id={chat}&name=report.html"
    # Already named, so the engine stores the bytes as sent.
    one, two = b'<h1 data-superchat-id="t">one</h1>', b'<h1 data-superchat-id="t">two</h1>'
    first = (await client.post(url, content=one)).json()
    second = (await client.post(url, content=two)).json()
    assert (first["version"], second["version"]) == (1, 2)
    assert second["kind"] == "html" and second["size"] == len(two)

    listed = (await client.get(f"/v1/artifacts?parent_session_id={chat}")).json()["artifacts"]
    assert [(a["id"], a["versions"]) for a in listed] == [(second["id"], 2)]

    meta = (await client.get(f"/v1/artifacts/{first['id']}")).json()
    assert [v["version"] for v in meta["all_versions"]] == [2, 1]

    latest = await client.get(f"/v1/artifacts/{second['id']}/content")
    assert latest.content == two
    assert latest.headers["content-type"].startswith("text/html")
    assert "sandbox allow-scripts" in latest.headers["content-security-policy"]
    assert latest.headers["content-disposition"].startswith("inline")
    old = await client.get(f"/v1/artifacts/{second['id']}/content?version=1&download=true")
    assert old.content == one
    assert old.headers["content-disposition"].startswith("attachment")

    assert (await client.delete(f"/v1/artifacts/{first['id']}")).json()["versions_removed"] == 2
    assert (await client.get(f"/v1/artifacts/{first['id']}")).status_code == 404


async def test_rejects_bad_type_and_empty(env: tuple[httpx.AsyncClient, str]) -> None:
    client, chat = env
    base = f"/v1/artifacts?parent_session_id={chat}"
    assert (await client.post(f"{base}&name=run.exe", content=b"x")).status_code == 400
    assert (await client.post(f"{base}&name=a.txt", content=b"")).status_code == 400
    assert (await client.get(f"/v1/artifacts/{'0' * 32}")).status_code == 404


class _ToolClient:
    """Adapts the in-process test client to the runner's ``server_client`` shape."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._c = client

    async def get(self, url: str, *, timeout: object = None) -> httpx.Response:
        return await self._c.get(url)

    async def delete(self, url: str, *, timeout: object = None) -> httpx.Response:
        return await self._c.delete(url)

    async def post(
        self,
        url: str,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        timeout: object = None,
    ) -> httpx.Response:
        return await self._c.post(url, content=content, headers=headers)


async def test_save_tool_reads_workspace_only(
    env: tuple[httpx.AsyncClient, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, chat = env
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "page.html").write_text("<p>hi</p>")
    (tmp_path / "secret.txt").write_text("nope")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setattr(handlers, "workspace_roots", lambda: [workspace.resolve()])

    async def _kind(*_a: object) -> tuple[str, str | None]:
        return "default", None

    from omnigent.superchat import _handler_http

    monkeypatch.setattr(_handler_http, "session_kind_and_parent", _kind)
    tc = _ToolClient(client)

    async def call(name: str, args: dict[str, object]) -> dict[str, object]:
        return json.loads(
            await handle_artifact_tool(HandlerCtx(name, tc, chat), args)  # type: ignore[arg-type]
        )

    saved = await call("artifact_save", {"path": "page.html", "title": "Page"})
    assert saved["type"] == "artifact" and saved["name"] == "page.html" and saved["version"] == 1
    again = await call("artifact_save", {"path": str(workspace / "page.html")})
    assert again["version"] == 2
    dotted = await call("artifact_save", {"path": "../secret.txt"})
    assert "workspace" in str(dotted["error"])
    outside = await call("artifact_save", {"path": str(tmp_path / "secret.txt")})
    assert "workspace" in str(outside["error"])
    assert "not found" in str(await call("artifact_save", {"path": "nope.html"}))
    listed = await call("artifact_list", {})
    assert [a["name"] for a in listed["artifacts"]] == ["page.html"]  # type: ignore[index]
    gone = await call("artifact_delete", {"artifact_id": str(saved["artifact_id"])})
    assert gone["deleted"] is True


async def test_delete_tool_always_asks() -> None:
    risk = classify_tool_call("artifact_delete", {"artifact_id": "a" * 32})
    assert risk is not None and risk.category == "delete"
    assert classify_tool_call("mcp__omnigent__artifact_delete", {}) is not None
    assert classify_tool_call("artifact_save", {"path": "a.html"}) is None


def _ask(conversations: SqlAlchemyConversationStore, chat: str, text: str) -> str:
    """A message of the person in *chat*; :returns: its item id."""
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


async def test_a_version_records_the_turn_and_chat_that_saved_it(
    env: tuple[httpx.AsyncClient, str],
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, chat = env
    conversations = SqlAlchemyConversationStore(db_uri)
    asked = _ask(conversations, chat, "make the page")
    url = f"/v1/artifacts?parent_session_id={chat}&name=page.html"
    first = (await client.post(url, content=b'<p data-superchat-id="p">1</p>')).json()

    # A Helper saves through the tool: the version names the Helper and its Brief.
    helper = conversations.create_conversation(parent_conversation_id=chat).id
    brief = _ask(conversations, helper, "update the page")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "page.html").write_text('<p data-superchat-id="p">2</p>')
    monkeypatch.setattr(handlers, "workspace_roots", lambda: [workspace.resolve()])

    async def _kind(*_a: object) -> tuple[str, str | None]:
        return "sub_agent", chat

    from omnigent.superchat import _handler_http

    monkeypatch.setattr(_handler_http, "session_kind_and_parent", _kind)
    ctx = HandlerCtx("artifact_save", _ToolClient(client), helper)  # type: ignore[arg-type]
    second = json.loads(await handle_artifact_tool(ctx, {"path": "page.html"}))
    assert second["version"] == 2

    # A chat outside the Conversation records nothing.
    stranger = conversations.create_conversation().id
    _ask(conversations, stranger, "elsewhere")
    third = (await client.post(f"{url}&chat_id={stranger}", content=b"<p>3</p>")).json()
    assert third["version"] == 3

    meta = (await client.get(f"/v1/artifacts/{first['id']}")).json()
    made_by = {v["version"]: (v["chat_id"], v["turn_item_id"]) for v in meta["all_versions"]}
    assert made_by == {1: (chat, asked), 2: (helper, brief), 3: (None, None)}
