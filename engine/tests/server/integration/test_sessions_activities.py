"""Integration tests for the Activity Feed routes.

``GET /v1/sessions/{id}/activities`` and
``GET /v1/sessions/{id}/activities/{activity_id}`` — the owner-scoped,
READ-gated wrapper around ``omnigent.superchat.activity.derive`` (derivation
unit-tested directly in tests/superchat/). These tests cover the route
wiring: auth/404 handling and the end-to-end shape over a seeded
Super Chat + Sub-agent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities import FunctionCallData, MessageData, NewConversationItem
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_MODE_LABEL = "omnigent.context.mode"
_MODE_VALUE = "superside-chat"


async def _create_session(client: httpx.AsyncClient, agent_name: str) -> dict[str, Any]:
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, f"session create failed: {resp.text}"
    return resp.json()


# ── 404 ──────────────────────────────────────────────────────────────────


async def test_activities_404_for_nonexistent_session(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/activities")
    assert resp.status_code == 404


async def test_activity_detail_404_for_nonexistent_session(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/activities/sub_agent:conv_x"
    )
    assert resp.status_code == 404


async def test_server_marks_a_new_child_session_as_launching(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent = await _create_session(client, "launch-parent")
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["id"],
            "initial_items": [],
        },
    )
    assert created.status_code == 201, created.text
    child = conv_store.get_conversation(created.json()["id"])
    assert child is not None
    assert child.labels.get("omnigent.subagent.launching") == "true"
    top = conv_store.get_conversation(parent["id"])
    assert top is not None
    assert "omnigent.subagent.launching" not in top.labels


async def test_activity_changes_stream_404_for_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/activities/stream")
    assert resp.status_code == 404


async def test_activity_changes_stream_is_not_taken_for_an_activity_id(
    client: httpx.AsyncClient,
) -> None:
    # A plain session has no feed to watch: the stream route answers (and ends) as a
    # stream, rather than the detail route reading "stream" as an unknown activity id.
    session = await _create_session(client, "plain-stream-agent")
    resp = await client.get(f"/v1/sessions/{session['id']}/activities/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == ""


# ── Non-superside-chat session: empty, not an error ──────────────────────


async def test_activities_empty_for_plain_session(client: httpx.AsyncClient) -> None:
    session = await _create_session(client, "plain-agent")
    resp = await client.get(f"/v1/sessions/{session['id']}/activities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == []


# ── Full shape over a seeded Super Chat + Sub-agent ───────────────────────


async def test_activities_list_and_detail_for_superside_chat_session(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.superchat.activity.derive._MIN_TURN_STEPS", 1)
    session = await _create_session(client, "superside-agent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_labels(session["id"], {_MODE_LABEL: _MODE_VALUE})

    response_id = "resp_1"
    conv_store.append(
        session["id"],
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "research auth flow mechanics"}],
                ),
            ),
            NewConversationItem(
                type="function_call",
                response_id=response_id,
                data=FunctionCallData(
                    agent="brain",
                    name="memory_search",
                    arguments=json.dumps({"query": "auth flow"}),
                    call_id="call_1",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="brain",
                    content=[{"type": "output_text", "text": "Researched auth flow mechanics"}],
                ),
            ),
        ],
    )
    sub_agent = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=session["id"],
        title="researcher:auth-flow",
        labels={_MODE_LABEL: _MODE_VALUE},
    )
    conv_store.set_session_live_status(sub_agent.id, "idle")

    list_resp = await client.get(f"/v1/sessions/{session['id']}/activities")
    assert list_resp.status_code == 200
    rows = list_resp.json()["data"]
    assert len(rows) == 2
    by_kind = {row["kind"]: row for row in rows}

    turn = by_kind["turn"]
    assert turn["chat_id"] == session["id"]
    assert turn["outcome"] == "Researched auth flow mechanics"
    assert turn["status"] == "done"
    assert "date" in turn
    assert turn["steps"][0]["title"] == "Searched memory for 'auth flow'"
    assert "detail" not in turn["steps"][0]

    sub = by_kind["sub_agent"]
    assert sub["chat_id"] == sub_agent.id
    assert sub["title"] == "auth-flow"
    assert sub["status"] == "done"

    detail_resp = await client.get(f"/v1/sessions/{session['id']}/activities/{sub['id']}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["id"] == sub["id"]
    assert detail["steps"] == []  # the seeded sub-agent had no tool calls


async def test_activity_detail_404_for_unknown_activity_id(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session = await _create_session(client, "superside-agent-2")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_labels(session["id"], {_MODE_LABEL: _MODE_VALUE})

    resp = await client.get(f"/v1/sessions/{session['id']}/activities/sub_agent:conv_missing")
    assert resp.status_code == 404


# ── Access: an app with permissions on ───────────────────────────────


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with header-mode auth + permission_store so access is gated."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(auth_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Transcript and family stream: same gates as the items / activities routes ──


def _family_with_users(db_uri: str) -> tuple[Any, str, str]:
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    super_chat = conv_store.create_conversation(labels={_MODE_LABEL: _MODE_VALUE})
    alice, bob = "alice@example.com", "bob@example.com"
    perm_store.ensure_user(alice)
    perm_store.ensure_user(bob)
    perm_store.grant(alice, super_chat.id, LEVEL_OWNER)
    return super_chat, alice, bob


async def test_transcript_is_gated_exactly_like_items(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    super_chat, alice, bob = _family_with_users(db_uri)
    owner = await auth_client.get(
        f"/v1/sessions/{super_chat.id}/transcript", headers={"X-Forwarded-Email": alice}
    )
    assert owner.status_code == 200, owner.text
    items_for_bob = await auth_client.get(
        f"/v1/sessions/{super_chat.id}/items", headers={"X-Forwarded-Email": bob}
    )
    transcript_for_bob = await auth_client.get(
        f"/v1/sessions/{super_chat.id}/transcript", headers={"X-Forwarded-Email": bob}
    )
    assert items_for_bob.status_code in (403, 404)
    assert transcript_for_bob.status_code == items_for_bob.status_code


async def test_family_stream_needs_read_on_the_super_chat(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Bob cannot read the Super Chat, so its family stream is hidden from him."""
    super_chat, _, bob = _family_with_users(db_uri)
    nothing = await auth_client.get(
        f"/v1/sessions/{super_chat.id}/family/stream", headers={"X-Forwarded-Email": bob}
    )
    assert nothing.status_code in (403, 404)
