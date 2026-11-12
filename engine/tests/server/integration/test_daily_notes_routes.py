"""Integration tests for the daily-note routes and the ``daily_note_*`` runner tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def env(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, str, str]]:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
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
    helper = conversations.create_conversation(
        kind="sub_agent", parent_conversation_id=chat, title="analyst:Study"
    ).id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, chat, helper


async def test_writer_merges_and_person_edit_wins(env: tuple[httpx.AsyncClient, str, str]) -> None:
    client, chat, _ = env
    written = await client.post(
        "/v1/daily-notes/write",
        json={"parent_session_id": chat, "sections": {"decisions": "- ship friday"}},
    )
    assert written.status_code == 200, written.text
    day = written.json()["date"]
    put = await client.put(
        f"/v1/me/daily-notes/{day}", json={"sections": {"decisions": "- ship monday"}}
    )
    assert put.json()["edited_by_person"] is True
    again = await client.post(
        "/v1/daily-notes/write",
        json={"parent_session_id": chat, "sections": {"decisions": "- ship friday\n- hire"}},
    )
    assert again.json()["sections"]["decisions"] == "- ship monday\n- ship friday\n- hire"
    today = await client.get("/v1/me/daily-notes/today")
    assert today.json()["date"] == day
    listed = await client.get("/v1/me/daily-notes", params={"from": day, "to": day})
    assert [n["date"] for n in listed.json()["daily_notes"]] == [day]


async def test_rejects_unknown_section_and_bad_date(
    env: tuple[httpx.AsyncClient, str, str],
) -> None:
    client, _, _ = env
    assert (
        await client.put("/v1/me/daily-notes/2026-10-04", json={"sections": {"x": "y"}})
    ).status_code == 400
    assert (await client.get("/v1/me/daily-notes/nope")).status_code == 400
