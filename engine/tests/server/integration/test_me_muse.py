"""``/v1/me/muse``, the session's ``superchat`` block, and read-only Helpers, with auth on.

Header-mode identity (as a proxy sends it) and an external IdP bearer JWT (offline JWKS,
generated key) both reach the same routes.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from omnigent.entities import MessageData, NewConversationItem
from omnigent.hosts.docker import DockerHostLauncher
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.jwt_bearer import JwtBearerConfig, JwtBearerVerifier
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.superchat.muse import MUSE_LABEL_KEY, muse_session_id
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio

ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}
ISSUER = "https://idp.example.test/"
AUDIENCE = "omnigent-api"
_MODE = {"omnigent.context.mode": "superside-chat"}


@pytest.fixture(scope="module")
def idp_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _verifier(private_key: rsa.RSAPrivateKey) -> JwtBearerVerifier:
    public = private_key.public_key()
    return JwtBearerVerifier(
        JwtBearerConfig(
            jwks_url="https://idp.example.test/jwks",
            issuer=ISSUER,
            audience=AUDIENCE,
            tenant_claim="org_id",
        ),
        signing_key=lambda _token: public,
    )


def _bearer(private_key: rsa.RSAPrivateKey, **claims: Any) -> dict[str, str]:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300, **claims}
    return {"Authorization": f"Bearer {jwt.encode(payload, private_key, algorithm='RS256')}"}


@pytest.fixture()
def stores(db_uri: str, tmp_path: Path) -> dict[str, Any]:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return {
        "agent": SqlAlchemyAgentStore(db_uri),
        "conversation": SqlAlchemyConversationStore(db_uri),
        "artifact": artifact_store,
    }


def _register_builtin(stores: dict[str, Any], agent_id: str, name: str) -> None:
    bundle = build_agent_bundle(name)
    key = f"{agent_id}/{hashlib.sha256(bundle).hexdigest()}"
    stores["artifact"].put(key, bundle)
    stores["agent"].create(agent_id, name, key)


@pytest.fixture()
def muse_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    stores: dict[str, Any],
    idp_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.delenv("OMNIGENT_AUTH_HEADER_SECRET", raising=False)
    monkeypatch.delenv("OMNIGENT_HOST_PROVIDER", raising=False)
    monkeypatch.setenv("OMNIGENT_SUPERCHAT_DEFAULT_AGENT", "muse")
    _register_builtin(stores, "a" * 32, "muse")
    _register_builtin(stores, "b" * 32, "muse-pro")
    return create_app(
        agent_store=stores["agent"],
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=stores["conversation"],
        artifact_store=stores["artifact"],
        agent_cache=AgentCache(artifact_store=stores["artifact"], cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(
            source="header",
            local_single_user=False,
            tenant_header="X-Omnigent-Tenant",
            jwt_bearer=_verifier(idp_key),
        ),
    )


@pytest_asyncio.fixture()
async def client(
    muse_app: FastAPI, mock_llm: ControllableMockClient, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)
    transport = httpx.ASGITransport(app=muse_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


# ── /v1/me/muse ───────────────────────────────────────────────────────────


async def test_get_creates_the_super_chat_once_then_finds_it(
    client: httpx.AsyncClient, stores: dict[str, Any]
) -> None:
    first = await client.get("/v1/me/muse", headers=ALICE)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body == {
        "session_id": muse_session_id("alice@example.com", None),
        "agent": "muse",
        "created": True,
    }
    again = (await client.get("/v1/me/muse", headers=ALICE)).json()
    assert again == {**body, "created": False}
    conv = stores["conversation"].get_conversation(body["session_id"])
    assert conv is not None
    assert conv.labels["omnigent.context.mode"] == "superside-chat"
    assert conv.labels[MUSE_LABEL_KEY] == "true"
    assert conv.labels["omnigent.superchat.muse.key"] == body["session_id"]
    # The host is keyed by the opaque Muse key, never the email.
    assert conv.labels["omnigent.host.owner"] == body["session_id"]


async def test_parallel_first_calls_create_one_super_chat(
    client: httpx.AsyncClient, stores: dict[str, Any]
) -> None:
    results = await asyncio.gather(*(client.get("/v1/me/muse", headers=ALICE) for _ in range(12)))
    bodies = [r.json() for r in results]
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({b["session_id"] for b in bodies}) == 1
    assert sum(b["created"] for b in bodies) == 1
    page = stores["conversation"].list_conversations(limit=100, kind=None)
    assert sum(1 for c in page.data if c.labels.get(MUSE_LABEL_KEY) == "true") == 1


async def test_each_user_and_tenant_has_their_own(client: httpx.AsyncClient) -> None:
    alice = (await client.get("/v1/me/muse", headers=ALICE)).json()["session_id"]
    alice_work = (
        await client.get("/v1/me/muse", headers={**ALICE, "X-Omnigent-Tenant": "space-1"})
    ).json()["session_id"]
    bob = (await client.get("/v1/me/muse", headers=BOB)).json()["session_id"]
    assert len({alice, alice_work, bob}) == 3
    assert (await client.get(f"/v1/sessions/{alice}", headers=BOB)).status_code == 404
    assert (await client.get("/v1/me/muse")).status_code == 401


async def test_not_configured_is_a_stable_code(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_SUPERCHAT_DEFAULT_AGENT")
    resp = await client.get("/v1/me/muse", headers=ALICE)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "superchat_not_configured"


async def test_put_switches_the_agent_and_is_idempotent(client: httpx.AsyncClient) -> None:
    created = (await client.get("/v1/me/muse", headers=ALICE)).json()
    switched = await client.put("/v1/me/muse", json={"agent": "muse-pro"}, headers=ALICE)
    assert switched.status_code == 200, switched.text
    assert switched.json() == {
        "session_id": created["session_id"],
        "agent": "muse-pro",
        "created": False,
    }
    assert (await client.get("/v1/me/muse", headers=ALICE)).json()["agent"] == "muse-pro"
    same = await client.put("/v1/me/muse", json={"agent": "muse-pro"}, headers=ALICE)
    assert same.status_code == 200 and same.json()["agent"] == "muse-pro"
    unknown = await client.put("/v1/me/muse", json={"agent": "nope"}, headers=ALICE)
    assert unknown.status_code == 404


async def test_a_harness_switch_drops_the_pinned_model_and_keeps_the_conversation(
    client: httpx.AsyncClient, stores: dict[str, Any]
) -> None:
    # A model id belongs to its harness's catalog: a Claude id pinned on a Claude Super Chat
    # must not follow it onto pi, while the conversation itself (same session) carries over.
    claude = {"type": "omnigent", "config": {"harness": "claude-sdk"}}
    pi = {"type": "omnigent", "config": {"harness": "pi"}}
    for agent_id, name, executor in (("c" * 32, "muse-claude", claude), ("d" * 32, "muse-pi", pi)):
        bundle = build_agent_bundle(name, executor=executor)
        key = f"{agent_id}/{hashlib.sha256(bundle).hexdigest()}"
        stores["artifact"].put(key, bundle)
        stores["agent"].create(agent_id, name, key)
    created = (
        await client.put("/v1/me/muse", json={"agent": "muse-claude"}, headers=ALICE)
    ).json()
    session_id = created["session_id"]
    pinned = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": "claude-opus-4"}, headers=ALICE
    )
    assert pinned.status_code == 200, pinned.text
    assert stores["conversation"].get_conversation(session_id).model_override == "claude-opus-4"
    said = MessageData(role="user", content=[{"type": "input_text", "text": "hello"}])
    (asked,) = stores["conversation"].append(
        session_id, [NewConversationItem(type="message", response_id="resp_1", data=said)]
    )

    switched = await client.put("/v1/me/muse", json={"agent": "muse-pi"}, headers=ALICE)
    assert switched.status_code == 200, switched.text
    assert switched.json()["session_id"] == session_id
    assert stores["conversation"].get_conversation(session_id).model_override is None
    items = stores["conversation"].list_items(session_id, limit=100).data
    assert asked.id in [item.id for item in items]


async def test_put_creates_on_the_requested_agent_when_missing(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.put("/v1/me/muse", json={"agent": "muse-pro"}, headers=BOB)
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent"] == "muse-pro" and resp.json()["created"] is True


async def test_a_bearer_jwt_finds_its_own_super_chat_per_tenant(
    client: httpx.AsyncClient, idp_key: rsa.RSAPrivateKey
) -> None:
    token = _bearer(idp_key, sub="u-1", email="carol@example.com", org_id="acme")
    resp = await client.get("/v1/me/muse", headers=token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_id"] == muse_session_id("carol@example.com", "acme")
    snap = await client.get(f"/v1/sessions/{resp.json()['session_id']}", headers=token)
    assert snap.status_code == 200
    assert snap.json()["labels"]["omnigent.tenant"] == "acme"
    expired = _bearer(idp_key, email="carol@example.com", exp=int(time.time()) - 3600)
    assert (await client.get("/v1/me/muse", headers=expired)).status_code == 401


# ── superchat block on GET /v1/sessions/{id} ───────────────────────────────


async def test_session_read_carries_kind_root_and_project(
    client: httpx.AsyncClient, stores: dict[str, Any]
) -> None:
    store: SqlAlchemyConversationStore = stores["conversation"]
    muse_id = (await client.get("/v1/me/muse", headers=ALICE)).json()["session_id"]
    muse = (await client.get(f"/v1/sessions/{muse_id}", headers=ALICE)).json()
    assert muse["superchat"] == {
        "kind": "super",
        "root_id": muse_id,
        "parent_id": None,
        "project": None,
        "folder": None,
        "workspace_chosen": False,
    }
    agent_id = muse["agent_id"]
    in_project = store.create_conversation(
        agent_id=agent_id,
        labels=_MODE,
        host_id="f" * 32,
        workspace="/home/u/workspace/projects/q3-deck",
    )
    store.set_labels(
        in_project.id,
        {
            "omnigent.project.name": "Q3 board deck",
            "omnigent.superchat.workspace.chosen": "person",
        },
    )
    SqlAlchemyPermissionStore(store.storage_location).grant("alice@example.com", in_project.id, 4)
    project_view = (await client.get(f"/v1/sessions/{in_project.id}", headers=ALICE)).json()
    assert project_view["superchat"] == {
        "kind": "super",
        "root_id": in_project.id,
        "parent_id": None,
        "project": {"slug": "q3-deck", "name": "Q3 board deck"},
        "folder": "projects/q3-deck",
        "workspace_chosen": True,
    }
    helper = store.create_conversation(
        kind="sub_agent", agent_id=agent_id, parent_conversation_id=muse_id, title="Job"
    )
    helper_view = (await client.get(f"/v1/sessions/{helper.id}", headers=ALICE)).json()
    assert helper_view["superchat"]["kind"] == "helper"
    assert helper_view["superchat"]["root_id"] == muse_id
    assert helper_view["superchat"]["parent_id"] == muse_id
    plain = store.create_conversation(agent_id=agent_id)
    SqlAlchemyPermissionStore(store.storage_location).grant("alice@example.com", plain.id, 4)
    plain_view = (await client.get(f"/v1/sessions/{plain.id}", headers=ALICE)).json()
    assert plain_view["superchat"] is None


# ── Helpers are read-only ─────────────────────────────────────────────────

_MESSAGE = {
    "type": "message",
    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
}


async def test_a_user_message_to_a_helper_is_refused(
    client: httpx.AsyncClient, stores: dict[str, Any]
) -> None:
    store: SqlAlchemyConversationStore = stores["conversation"]
    muse_id = (await client.get("/v1/me/muse", headers=ALICE)).json()["session_id"]
    muse = store.get_conversation(muse_id)
    assert muse is not None
    helper = store.create_conversation(
        kind="sub_agent", agent_id=muse.agent_id, parent_conversation_id=muse_id, title="Job"
    )
    resp = await client.post(f"/v1/sessions/{helper.id}/events", json=_MESSAGE, headers=ALICE)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "helper_read_only"
    # Other events (stop, interrupt) are not messages and stay allowed.
    interrupt = await client.post(
        f"/v1/sessions/{helper.id}/events", json={"type": "interrupt", "data": {}}, headers=ALICE
    )
    assert interrupt.status_code != 403


async def test_the_parent_runner_may_still_brief_its_helper(
    client: httpx.AsyncClient, stores: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store: SqlAlchemyConversationStore = stores["conversation"]
    muse_id = (await client.get("/v1/me/muse", headers=ALICE)).json()["session_id"]
    binding = "runner-binding-token-for-test"
    assert store.set_runner_id(muse_id, token_bound_runner_id(binding))
    muse = store.get_conversation(muse_id)
    assert muse is not None
    helper = store.create_conversation(
        kind="sub_agent", agent_id=muse.agent_id, parent_conversation_id=muse_id, title="Job"
    )
    resp = await client.post(
        f"/v1/sessions/{helper.id}/events",
        json=_MESSAGE,
        headers={**ALICE, RUNNER_TUNNEL_TOKEN_HEADER: binding},
    )
    # Past the Helper gate: the refusal is dispatch's (no runner in this test), not read-only.
    assert resp.status_code != 403, resp.text
    wrong_token = await client.post(
        f"/v1/sessions/{helper.id}/events",
        json=_MESSAGE,
        headers={**ALICE, RUNNER_TUNNEL_TOKEN_HEADER: "some-other-runner"},
    )
    assert wrong_token.json()["error"]["code"] == "helper_read_only"


async def test_messages_to_a_plain_sub_agent_are_unchanged(
    client: httpx.AsyncClient, stores: dict[str, Any]
) -> None:
    store: SqlAlchemyConversationStore = stores["conversation"]
    agent = stores["agent"].get_by_name("muse")
    parent = store.create_conversation(agent_id=agent.id)
    SqlAlchemyPermissionStore(store.storage_location).grant("alice@example.com", parent.id, 4)
    child = store.create_conversation(
        kind="sub_agent", agent_id=agent.id, parent_conversation_id=parent.id, title="Job"
    )
    resp = await client.post(f"/v1/sessions/{child.id}/events", json=_MESSAGE, headers=ALICE)
    assert resp.status_code != 403, resp.text


async def test_a_call_that_loses_the_create_race_returns_the_winner(
    client: httpx.AsyncClient, stores: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary key settles a race: the late creator reads the row the winner wrote."""
    store: SqlAlchemyConversationStore = stores["conversation"]
    winner = (await client.get("/v1/me/muse", headers=ALICE)).json()
    real_get = store.get_conversation
    misses = {"left": 1}

    def stale_get(conversation_id: str) -> Any:
        if conversation_id == winner["session_id"] and misses["left"]:
            misses["left"] -= 1
            return None  # this caller looked before the winner's row landed
        return real_get(conversation_id)

    monkeypatch.setattr(store, "get_conversation", stale_get)
    # This caller's label lookup ran before the winner's row landed, too.
    monkeypatch.setattr(store, "find_conversation_ids_by_label", lambda *a, **k: [])
    loser = await client.get("/v1/me/muse", headers=ALICE)
    assert misses["left"] == 0
    assert loser.status_code == 200, loser.text
    assert loser.json() == {**winner, "created": False}


# ── adopting an existing Super Chat ───────────────────────────────────────

ALICE_SPACE_1 = {**ALICE, "X-Omnigent-Tenant": "space-1"}


async def _existing_super_chat(
    client: httpx.AsyncClient, headers: dict[str, str], space: str = "space-1"
) -> str:
    """A Super Chat made the way a client made them before ``/v1/me/muse`` (own labels)."""
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": "a" * 32,
            "labels": {**_MODE, "omnigent.host.key": "host-key-1", "omnigent.tenant": space},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_adopt_makes_an_existing_super_chat_the_one_get_returns(
    client: httpx.AsyncClient, stores: dict[str, Any]
) -> None:
    session_id = await _existing_super_chat(client, ALICE)
    adopted = await client.post(
        "/v1/me/muse/adopt", json={"session_id": session_id}, headers=ALICE_SPACE_1
    )
    assert adopted.status_code == 200, adopted.text
    assert adopted.json() == {"session_id": session_id, "agent": "muse", "created": False}
    found = (await client.get("/v1/me/muse", headers=ALICE_SPACE_1)).json()
    assert found == {"session_id": session_id, "agent": "muse", "created": False}
    again = await client.post(
        "/v1/me/muse/adopt", json={"session_id": session_id}, headers=ALICE_SPACE_1
    )
    assert again.status_code == 200 and again.json()["session_id"] == session_id

    conv = stores["conversation"].get_conversation(session_id)
    assert conv is not None
    assert conv.labels[MUSE_LABEL_KEY] == "true"
    assert conv.labels["omnigent.tenant"] == "space-1"
    assert conv.labels["omnigent.host.key"] == "host-key-1"
    # The host launcher still resolves the same host from the kept labels.
    launcher = DockerHostLauncher()
    launcher.prepare_for_launch(labels=conv.labels)
    assert (launcher._key, launcher._tenant) == ("host-key-1", "space-1")


async def test_adopt_someone_elses_session_is_not_found(client: httpx.AsyncClient) -> None:
    session_id = await _existing_super_chat(client, ALICE)
    resp = await client.post("/v1/me/muse/adopt", json={"session_id": session_id}, headers=BOB)
    assert resp.status_code == 404


async def test_adopt_refuses_a_plain_session_and_a_helper(
    client: httpx.AsyncClient, stores: dict[str, Any]
) -> None:
    store: SqlAlchemyConversationStore = stores["conversation"]
    root = await _existing_super_chat(client, ALICE)
    plain = store.create_conversation(agent_id="a" * 32)
    SqlAlchemyPermissionStore(store.storage_location).grant("alice@example.com", plain.id, 4)
    helper = store.create_conversation(
        kind="sub_agent", agent_id="a" * 32, parent_conversation_id=root, title="Job"
    )
    SqlAlchemyPermissionStore(store.storage_location).grant("alice@example.com", helper.id, 4)
    for session_id in (plain.id, helper.id):
        resp = await client.post(
            "/v1/me/muse/adopt", json={"session_id": session_id}, headers=ALICE_SPACE_1
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "not_a_super_chat"


async def test_adopt_when_another_muse_is_already_set_conflicts(
    client: httpx.AsyncClient,
) -> None:
    made = (await client.get("/v1/me/muse", headers=ALICE_SPACE_1)).json()["session_id"]
    other = await _existing_super_chat(client, ALICE)
    resp = await client.post(
        "/v1/me/muse/adopt", json={"session_id": other}, headers=ALICE_SPACE_1
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "muse_already_set"
    assert (await client.get("/v1/me/muse", headers=ALICE_SPACE_1)).json()["session_id"] == made


async def test_adopt_from_another_tenant_conflicts(client: httpx.AsyncClient) -> None:
    session_id = await _existing_super_chat(client, ALICE, space="space-2")
    resp = await client.post(
        "/v1/me/muse/adopt", json={"session_id": session_id}, headers=ALICE_SPACE_1
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "muse_tenant_mismatch"


async def test_an_adopted_muse_is_per_tenant(client: httpx.AsyncClient) -> None:
    session_id = await _existing_super_chat(client, ALICE)
    await client.post("/v1/me/muse/adopt", json={"session_id": session_id}, headers=ALICE_SPACE_1)
    elsewhere = (
        await client.get("/v1/me/muse", headers={**ALICE, "X-Omnigent-Tenant": "space-9"})
    ).json()
    assert elsewhere["session_id"] != session_id and elsewhere["created"] is True
