"""``GET /v1/sessions/{id}/items/search`` — the session_history REST search route.

The runner has no in-process ConversationStore, so the native-relay
dispatch for ``session_history``'s ``search`` action calls this endpoint
(see ``omnigent.runner.tool_dispatch._session_history_search_via_rest``).
This is its REST counterpart to ``ConversationStore.search``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.context.labels import SCHEDULED_HELPER_LABEL_KEY
from omnigent.entities import CompactionData, NewConversationItem
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, name: str) -> str:
    agent = await create_test_agent(client, name=name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _post_user_message(client: httpx.AsyncClient, session_id: str, text: str) -> None:
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
                "response_id": "resp_test",
            },
        },
    )
    assert resp.status_code in (200, 201, 202), resp.text


async def test_search_finds_own_session_item(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "search-route-own")
    await _post_user_message(client, session_id, "findableneedle in this session")

    resp = await client.get(
        f"/v1/sessions/{session_id}/items/search",
        params={"query": "findableneedle"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["type"] == "message"


async def test_search_matches_hyphenated_identifiers(client: httpx.AsyncClient) -> None:
    """Ids like account or reference numbers must not be parsed as FTS syntax."""
    session_id = await _create_session(client, "search-route-hyphen")
    await _post_user_message(client, session_id, "My codeword is CW-CODEX-A7E00A: keep it.")

    resp = await client.get(
        f"/v1/sessions/{session_id}/items/search",
        params={"query": "CW-CODEX-A7E00A:"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1


async def test_search_is_scoped_to_the_session(client: httpx.AsyncClient) -> None:
    session_a = await _create_session(client, "search-route-a")
    session_b = await _create_session(client, "search-route-b")
    await _post_user_message(client, session_a, "onlyinsessionamarker present here")
    await _post_user_message(client, session_b, "onlyinsessionamarker present here too")

    resp = await client.get(
        f"/v1/sessions/{session_a}/items/search",
        params={"query": "onlyinsessionamarker"},
    )
    assert resp.status_code == 200, resp.text
    # Both sessions contain the term, but the search is scoped to session_a —
    # a leak would return 2.
    assert len(resp.json()["data"]) == 1


async def test_search_missing_session_returns_404(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/v1/sessions/conv_does_not_exist/items/search",
        params={"query": "anything"},
    )
    assert resp.status_code == 404


async def test_search_requires_nonempty_query(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "search-route-empty-query")
    resp = await client.get(f"/v1/sessions/{session_id}/items/search", params={"query": ""})
    assert resp.status_code == 422


async def test_search_limit_is_capped_at_20(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "search-route-limit")
    resp = await client.get(
        f"/v1/sessions/{session_id}/items/search",
        params={"query": "x", "limit": 1000},
    )
    assert resp.status_code == 422


# ── GET /sessions/{id}/related_chats — session_history's list_chats ────


def _helper_of(store: SqlAlchemyConversationStore, parent_id: str) -> str:
    return store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent_id,
        title="analyst:Daily note",
        labels={SCHEDULED_HELPER_LABEL_KEY: "t1"},
    ).id


async def test_related_chats_lists_a_scheduled_helpers_parent(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent_id = await _create_session(client, "related-chats-parent")
    helper_id = _helper_of(store, parent_id)
    owner = store.get_session_owner(parent_id)
    if owner is not None:
        SqlAlchemyPermissionStore(db_uri).grant(owner, helper_id, LEVEL_OWNER)

    resp = await client.get(f"/v1/sessions/{helper_id}/related_chats")
    assert resp.status_code == 200, resp.text
    [row] = resp.json()["data"]
    assert row["id"] == parent_id and row["live"] is False

    store.set_session_live_status(parent_id, "running")
    resp = await client.get(f"/v1/sessions/{helper_id}/related_chats")
    assert resp.json()["data"][0]["live"] is True


async def test_related_chats_is_empty_for_a_top_level_chat(client: httpx.AsyncClient) -> None:
    source_id = await _create_session(client, "related-chats-top-level")
    resp = await client.get(f"/v1/sessions/{source_id}/related_chats")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


async def test_related_chats_missing_session_returns_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/conv_does_not_exist/related_chats")
    assert resp.status_code == 404


# ── GET /sessions/{id}/context_summary — "Knows our conversation" hover ──


async def test_context_summary_is_null_without_a_rollover(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "context-summary-none")

    resp = await client.get(f"/v1/sessions/{session_id}/context_summary")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"summary": None, "summary_body": None, "created_at": None}


async def test_context_summary_reports_the_latest_checkpoint(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = await _create_session(client, "context-summary-latest")
    store = SqlAlchemyConversationStore(db_uri)
    store.append(
        session_id,
        [
            NewConversationItem(
                type="compaction",
                response_id="resp_seed_1",
                data=CompactionData(
                    summary="First checkpoint.",
                    last_item_id="msg_1",
                    model=None,
                    token_count=10,
                ),
            )
        ],
    )
    store.append(
        session_id,
        [
            NewConversationItem(
                type="compaction",
                response_id="resp_seed_2",
                data=CompactionData(
                    summary="Second, newer checkpoint.",
                    last_item_id="msg_2",
                    model=None,
                    token_count=20,
                ),
            )
        ],
    )

    resp = await client.get(f"/v1/sessions/{session_id}/context_summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"] == "Second, newer checkpoint."
    assert body["summary_body"] == "Second, newer checkpoint."
    assert isinstance(body["created_at"], int)


async def test_context_summary_missing_session_returns_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/conv_does_not_exist/context_summary")
    assert resp.status_code == 404


# ── related_chats: the parent is re-checked against the caller ──────────────────


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


async def test_related_chats_needs_read_on_the_conversation(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A Helper is read through its Conversation's grants: Bob, granted only the Helper
    row, cannot list what it reads; Alice, who owns both, sees the parent."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)

    super_chat = conv_store.create_conversation()
    helper_id = _helper_of(conv_store, super_chat.id)

    alice, bob = "alice@example.com", "bob@example.com"
    perm_store.ensure_user(alice)
    perm_store.ensure_user(bob)
    perm_store.grant(alice, super_chat.id, LEVEL_OWNER)
    perm_store.grant(alice, helper_id, LEVEL_OWNER)
    perm_store.grant(bob, helper_id, LEVEL_READ)

    resp = await auth_client.get(
        f"/v1/sessions/{helper_id}/related_chats",
        headers={"X-Forwarded-Email": bob},
    )
    assert resp.status_code in (403, 404), resp.text

    alice_resp = await auth_client.get(
        f"/v1/sessions/{helper_id}/related_chats",
        headers={"X-Forwarded-Email": alice},
    )
    assert alice_resp.status_code == 200, alice_resp.text
    assert [c["id"] for c in alice_resp.json()["data"]] == [super_chat.id]
