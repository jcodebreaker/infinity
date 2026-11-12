"""Organization admin: people and usage, suspend/resume and delete."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.db_models import OmnigentBase, SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
)
from omnigent.errors import OmnigentError
from omnigent.runtime.public_error_codes import classify_provider_failure
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import Host
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.superchat.admin.routes import create_org_admin_router
from omnigent.superchat.models import budget as budget_module
from omnigent.superchat.models import org as org_module
from omnigent.superchat.models.budget import Budget, ModelBudgets, ModelBudgetStore
from omnigent.superchat.models.org import (
    SuspensionStore,
    bind_suspensions,
    refuse_if_suspended,
)
from omnigent.superchat.models.store import ModelConnectionStore
from omnigent.superchat.vault.store import VAULT_KEY_ENV

from .test_byok_pi import _hosted_state, _service
from .test_model_proxy import store  # noqa: F401  (the proxy's seeded connection store)

# 2026-10-14 12:26 UTC
NOW = 1_792_000_000
ROOT = {"x-user": "root"}
ALICE = {"x-user": "alice"}


class _Auth:
    """The caller is whoever the X-User header names."""

    def __init__(self) -> None:
        self.revoked: list[str] = []

    def get_user_id(self, request: Request) -> str:
        return request.headers.get("x-user", "alice")

    def revoke_user_sessions(self, user_id: str) -> None:
        self.revoked.append(user_id)


class _Hosts:
    """A host store with fixed hosts per user."""

    def __init__(self, hosts: dict[str, list[Host]]) -> None:
        self.hosts = hosts

    def list_hosts(self, user_id: str) -> list[Host]:
        return self.hosts.get(user_id, [])


def _host(name: str, provider: str | None, status: str = "online") -> Host:
    return Host(
        host_id=f"host-{name}",
        name=name,
        user_id="alice",
        status=status,
        created_at=NOW - 100,
        updated_at=NOW,
        sandbox_provider=provider,
        sandbox_id="sb-1" if provider else None,
    )


class World(SimpleNamespace):
    """Everything a test pokes at."""

    client: TestClient
    uri: str
    perms: SqlAlchemyPermissionStore
    ledger: SqlAlchemyConversationStore
    connections: ModelConnectionStore
    budgets: ModelBudgets
    suspensions: SuspensionStore
    auth: _Auth
    hosts: _Hosts
    terminated: list[str]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(budget_module, "now_epoch", lambda: NOW)
    monkeypatch.setattr(org_module, "now_epoch", lambda: NOW)
    uri = f"sqlite:///{tmp_path / 'o.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(uri))
    perms = SqlAlchemyPermissionStore(uri)
    for user, admin in (("root", True), ("admin2", True), ("alice", False), ("bob", False)):
        perms.ensure_user(user, is_admin=admin)
    ledger = SqlAlchemyConversationStore(uri)
    connections = ModelConnectionStore(uri)
    suspensions = SuspensionStore(uri)
    budgets = ModelBudgets(ModelBudgetStore(uri), ledger)
    auth = _Auth()
    hosts = _Hosts({})
    terminated: list[str] = []

    async def terminate(host: Host, host_store: Any, config: Any) -> None:
        terminated.append(host.host_id)
        hosts.hosts[host.user_id] = [h for h in hosts.hosts[host.user_id] if h is not host]

    monkeypatch.setattr("omnigent.server.hosts.terminate_host", terminate)

    state = _hosted_state({})

    class _Keys:
        def get_plaintext(self, scope, owner_id, preferred):
            return ("anthropic", "sk-ant-x") if scope == "user" else None

    app = FastAPI()
    app.state.sandbox_config = state.sandbox_config
    app.state.databricks_store = None
    app.state.databricks_client = None
    app.state.model_connection_store = _Keys()
    app.state.host_store = hosts
    # The template names no model: catalogs are the key's own listing.
    app.state.inference_catalog = _service(app.state)
    app.include_router(
        create_org_admin_router(
            suspensions=suspensions,
            budgets=budgets,
            connections=connections,
            conversation_store=ledger,
            scheduled_task_store=SimpleNamespace(list=lambda owner_user_id: []),
            auth_provider=auth,  # type: ignore[arg-type]
            permission_store=perms,
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            {"code": str(exc.code), "message": str(exc)}, status_code=exc.http_status
        )

    bind_suspensions(suspensions)
    yield_world = World(
        client=TestClient(app),
        uri=uri,
        perms=perms,
        ledger=ledger,
        connections=connections,
        budgets=budgets,
        suspensions=suspensions,
        auth=auth,
        hosts=hosts,
        terminated=terminated,
        app=app,
    )
    yield yield_world
    bind_suspensions(None)


# ---- gating -----------------------------------------------------------------------------

ROUTES = [
    ("GET", "/v1/admin/users"),
    ("GET", "/v1/admin/usage"),
    ("POST", "/v1/admin/users/bob/suspend"),
    ("POST", "/v1/admin/users/bob/resume"),
    ("DELETE", "/v1/admin/users/bob"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_needs_an_admin_with_a_neutral_message(
    world: World, method: str, path: str
) -> None:
    r = world.client.request(method, path, headers=ALICE, json={"harnesses": {}})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert "organization" in r.json()["message"] and "polic" not in r.json()["message"]
    assert world.perms.get_user("bob") is not None  # nothing happened
    assert world.suspensions.suspended_since() == {}


# ---- people and usage -------------------------------------------------------------------


def _seed_people(world: World) -> None:
    world.ledger.add_daily_cost("alice", "2026-10-14", 1.5)
    world.ledger.add_daily_cost("alice", "2026-10-02", 0.5)
    world.ledger.add_daily_cost("bob", "2026-10-14", 4.0)
    world.ledger.add_daily_cost("bob", "2026-09-30", 9.0)  # last month: not counted
    world.connections.put("user", "alice", "anthropic", "sk-ant-secret-1234")
    world.budgets.store.set("alice", Budget(25.0, "stop"))
    world.suspensions.suspend("bob", "root")
    world.hosts.hosts["alice"] = [_host("c1", "docker"), _host("laptop", None, "offline")]
    conv = world.ledger.create_conversation()
    world.perms.grant("alice", conv.id, LEVEL_OWNER)
    child = world.ledger.create_conversation(parent_conversation_id=conv.id)
    world.perms.grant("alice", child.id, LEVEL_OWNER)  # sub-agents are not counted


def test_users_list_every_person_with_stats(world: World) -> None:
    _seed_people(world)
    users = {u["id"]: u for u in world.client.get("/v1/admin/users", headers=ROOT).json()["users"]}
    assert set(users) == {"root", "admin2", "alice", "bob"}
    alice = users["alice"]
    assert alice["status"] == "active" and alice["is_admin"] is False
    assert alice["spend_month_usd"] == pytest.approx(2.0)
    assert alice["spend_today_usd"] == pytest.approx(1.5)
    assert alice["session_count"] == 1 and alice["last_active"] is not None
    assert alice["budget"] == {"monthly_limit_usd": 25.0, "at_limit": "stop"}
    assert alice["model_connections"] == [
        {"provider": "anthropic", "hint": "1234", "status": "valid"}
    ]
    assert "sk-ant-secret" not in str(users)
    assert alice["host"]["provider"] == "docker" and alice["host"]["managed"] is True
    assert alice["host"]["state"] in ("online", "offline")
    bob = users["bob"]
    assert bob["status"] == "suspended" and bob["suspended_at"] == NOW
    assert bob["spend_month_usd"] == pytest.approx(4.0) and bob["spend_today_usd"] == 4.0
    assert bob["host"] is None and bob["model_connections"] == []
    assert users["root"]["is_admin"] is True and users["root"]["session_count"] == 0
    assert users["root"]["spend_month_usd"] == 0.0


def test_usage_windows_give_org_totals_per_person_and_per_day(world: World) -> None:
    _seed_people(world)
    world.budgets.store.set("__workspace__", Budget(100.0, "stop"))
    month = world.client.get("/v1/admin/usage", headers=ROOT).json()
    assert month["window"] == "month" and month["period_start"] == "2026-10-01"
    assert month["total_usd"] == pytest.approx(6.0)
    assert month["by_user"] == [
        {"user_id": "bob", "cost_usd": 4.0},
        {"user_id": "alice", "cost_usd": 2.0},
    ]
    assert month["by_day"] == [
        {"day": "2026-10-02", "cost_usd": 0.5},
        {"day": "2026-10-14", "cost_usd": 5.5},
    ]
    assert month["org_budget"] == {"monthly_limit_usd": 100.0, "at_limit": "stop"}
    day = world.client.get("/v1/admin/usage?window=day", headers=ROOT).json()
    assert day["period_start"] == "2026-10-14" and day["total_usd"] == pytest.approx(5.5)
    assert day["by_day"] == [{"day": "2026-10-14", "cost_usd": 5.5}]
    bad = world.client.get("/v1/admin/usage?window=year", headers=ROOT)
    assert bad.status_code == 400


# ---- suspend and resume -----------------------------------------------------------------


def test_suspend_flags_the_user_and_resume_restores(world: World) -> None:
    r = world.client.post("/v1/admin/users/alice/suspend", headers=ROOT)
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert r.json()["sessions_interrupted"] == 0
    assert world.suspensions.is_suspended("alice") and not world.suspensions.is_suspended("bob")
    # idempotent, and the first timestamp is kept
    assert world.client.post("/v1/admin/users/alice/suspend", headers=ROOT).status_code == 200
    users = world.client.get("/v1/admin/users", headers=ROOT).json()["users"]
    assert {u["id"]: u["status"] for u in users}["alice"] == "suspended"
    resumed = world.client.post("/v1/admin/users/alice/resume", headers=ROOT)
    assert resumed.json() == {"id": "alice", "status": "active"}
    assert not world.suspensions.is_suspended("alice")


def test_cannot_suspend_yourself_the_last_active_admin_or_a_stranger(world: World) -> None:
    mine = world.client.post("/v1/admin/users/root/suspend", headers=ROOT)
    assert mine.status_code == 400 and mine.json()["code"] == "invalid_input"
    assert world.client.post("/v1/admin/users/ghost/suspend", headers=ROOT).status_code == 404
    assert (
        world.client.post("/v1/admin/users/__workspace__/suspend", headers=ROOT).status_code == 404
    )
    # root suspends admin2; the suspended admin2 may not then suspend the last active admin.
    assert world.client.post("/v1/admin/users/admin2/suspend", headers=ROOT).status_code == 200
    last = world.client.post("/v1/admin/users/root/suspend", headers={"x-user": "admin2"})
    assert last.status_code == 400 and "last active admin" in last.json()["message"]
    assert not world.suspensions.is_suspended("root")


@pytest.mark.asyncio
async def test_suspended_users_are_refused_with_a_public_code(world: World) -> None:
    await refuse_if_suspended("alice")  # not suspended: passes
    await refuse_if_suspended(None)
    world.suspensions.suspend("alice", "root")
    with pytest.raises(OmnigentError) as caught:
        await refuse_if_suspended("alice")
    assert caught.value.code == "account_suspended" and caught.value.http_status == 403
    await refuse_if_suspended("bob")
    world.suspensions.resume("alice")
    await refuse_if_suspended("alice")


def test_the_suspension_code_is_classified_as_itself_not_as_a_rejected_key() -> None:
    assert classify_provider_failure(error_type="account_suspended", status=403) == (
        "account_suspended"
    )
    assert classify_provider_failure(status=403) == "auth_failed"


def test_suspension_row_is_a_preference_and_dies_with_the_account(world: World) -> None:
    world.suspensions.suspend("alice", "root")
    maker = make_named_managed_session_maker(
        get_or_create_engine(world.uri), query_name_prefix="t"
    )
    with maker("t") as session:
        assert (
            session.get(SqlPreference, (current_workspace_id(), "alice", "account_suspended"))
            is not None
        )


# ---- delete -----------------------------------------------------------------------------


def _preference_keys(world: World, user: str) -> set[str]:
    from sqlalchemy import select

    maker = make_named_managed_session_maker(
        get_or_create_engine(world.uri), query_name_prefix="t"
    )
    with maker("t") as session:
        return set(
            session.scalars(
                select(SqlPreference.key).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == user,
                )
            )
        )


def test_delete_removes_the_person_and_everything_they_own_and_is_idempotent(
    world: World,
) -> None:
    from omnigent.superchat.models.selection import ModelPreferenceStore

    _seed_people(world)
    world.hosts.hosts["alice"] = [_host("c1", "docker"), _host("laptop", None)]
    world.connections.put("user", "alice", "openrouter", "sk-or-secret-5678")
    ModelPreferenceStore(world.uri).set("alice", foundation={"claude-sdk": "claude-haiku-4-5"})
    world.budgets.store.set_approval("alice", "2026-10", "user", 1.25)
    owned = [
        c.id for c in world.ledger.list_conversations(owned_by="alice", kind=None, limit=10).data
    ]
    assert len(owned) == 2  # the session and its sub-agent

    r = world.client.delete("/v1/admin/users/alice", headers=ROOT)
    assert r.status_code == 200
    body = r.json()
    # What the schema-wide sweep found after the dedicated steps; its exact count is not the point.
    assert body.pop("rows_removed") >= 0
    assert body == {
        "id": "alice",
        "deleted": True,
        "sessions_deleted": 1,
        "hosts_stopped": 1,
        "model_connections_deleted": 2,
        "memory_claims_forgotten": 0,  # no memory service in this world
    }
    assert world.terminated == ["host-c1"]  # the managed host only; no user-owned host left
    assert world.perms.get_user("alice") is None
    assert world.connections.list("user", "alice") == []
    assert _preference_keys(world, "alice") == set()  # budget, defaults, approvals, flag
    assert world.ledger.sum_daily_cost("alice", "2026-01-01") == 0.0
    assert world.auth.revoked == ["alice"]
    for conversation_id in owned:
        assert world.ledger.get_conversation(conversation_id) is None
    users = world.client.get("/v1/admin/users", headers=ROOT).json()["users"]
    assert "alice" not in {u["id"] for u in users}
    # bob and the organization are untouched
    assert world.suspensions.is_suspended("bob")
    assert world.ledger.sum_daily_cost("bob", "2026-10-01") == pytest.approx(4.0)

    again = world.client.delete("/v1/admin/users/alice", headers=ROOT)
    assert again.status_code == 200
    assert again.json()["deleted"] is False and again.json()["sessions_deleted"] == 0
    assert world.suspensions.suspended_since() == {"bob": NOW}  # no stray flag for the gone
    assert world.terminated == ["host-c1"]


def test_delete_refuses_yourself_and_reserved_identities(world: World) -> None:
    mine = world.client.delete("/v1/admin/users/root", headers=ROOT)
    assert mine.status_code == 400 and mine.json()["code"] == "invalid_input"
    assert world.perms.get_user("root") is not None
    reserved = world.client.delete("/v1/admin/users/__workspace__", headers=ROOT)
    assert reserved.status_code == 404


def test_an_admin_can_delete_another_admin_while_one_remains(world: World) -> None:
    r = world.client.delete("/v1/admin/users/admin2", headers=ROOT)
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert world.perms.is_admin("root")


# ---- choke points: session create, turns and the model proxy -----------------------------


@pytest.mark.asyncio
async def test_a_suspended_user_cannot_create_a_session_or_send_a_turn(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions import routes_core, routes_events
    from tests.server.helpers import create_test_agent

    # The test app has no login; name the caller so there is someone to suspend.
    monkeypatch.setattr(routes_core, "_require_user", lambda *_: "alice")
    monkeypatch.setattr(routes_events, "_get_user_id", lambda *_: "alice")

    agent = await create_test_agent(client, name="suspend-test")
    made = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert made.status_code == 201
    session_id = made.json()["id"]

    bind_suspensions(SimpleNamespace(is_suspended=lambda _user: True))  # type: ignore[arg-type]
    try:
        refused = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "account_suspended"
        turn = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "message", "data": {"content": "hi"}},
        )
        assert turn.status_code == 403
        assert turn.json()["error"]["code"] == "account_suspended"
    finally:
        bind_suspensions(None)
    # reads still work for a suspended user, and resuming restores creation
    assert (await client.get(f"/v1/sessions/{session_id}")).status_code == 200
    again = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert again.status_code == 201


def test_the_model_proxy_refuses_a_suspended_owner_until_resumed(
    store,  # noqa: F811
    tmp_path: Path,
) -> None:
    from .test_model_proxy import CRED, _client

    uri = f"sqlite:///{tmp_path / 'susp.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(uri))
    suspensions = SuspensionStore(uri)
    bind_suspensions(suspensions)
    try:
        _proxy_round_trip(store, suspensions, CRED, _client)
    finally:
        bind_suspensions(None)


def _proxy_round_trip(keys, suspensions, CRED, _client) -> None:
    seen: list = []
    client = _client(keys, seen)
    body = {"model": "claude-sonnet-5-5"}
    headers = {"x-api-key": CRED}
    assert (
        client.post("/v1/model/anthropic/v1/messages", json=body, headers=headers).status_code
        == 200
    )

    suspensions.suspend("alice", "root")  # the proxy's owner for CRED
    refused = client.post("/v1/model/anthropic/v1/messages", json=body, headers=headers)
    assert refused.status_code == 403
    error = refused.json()["error"]
    assert error["type"] == error["code"] == "account_suspended"
    assert "suspended" in error["message"]
    assert classify_provider_failure(error_type=error["type"], status=403) == "account_suspended"
    assert len(seen) == 1  # nothing reached the provider

    suspensions.resume("alice")
    assert (
        client.post("/v1/model/anthropic/v1/messages", json=body, headers=headers).status_code
        == 200
    )


def test_delete_leaves_no_skills_artifacts_publications_or_blobs_for_a_returning_account(
    world: World,
) -> None:
    """A reset account reuses the email, so whatever the engine kept would come back with it."""
    import uuid

    from sqlalchemy import insert, select

    tables = OmnigentBase.metadata.tables
    engine = get_or_create_engine(world.uri)
    deleted_blobs: list[str] = []
    world.client.app.state.artifact_store = SimpleNamespace(delete=deleted_blobs.append)
    _seed_people(world)

    def seed(user: str) -> None:
        artifact, parent = (uuid.uuid4().hex for _ in range(2))
        slug = f"app-{user}-{uuid.uuid4().hex[:6]}"
        with engine.begin() as c:
            c.execute(
                insert(tables["artifacts"]).values(
                    id=artifact,
                    user_id=user,
                    parent_session_id=parent,
                    name="a.html",
                    kind="html",
                    mime="text/html",
                    size=1,
                    blob_key=f"blob-{user}",
                    created_at=1,
                )
            )
            c.execute(
                insert(tables["artifact_publications"]).values(
                    slug=slug,
                    user_id=user,
                    parent_session_id=parent,
                    name="a.html",
                    audience="link",
                    published_version=1,
                    published_at=1,
                )
            )
            c.execute(
                insert(tables["artifact_views"]).values(
                    slug=slug, day="2026-10-01", viewer_key="v", last_at=1
                )
            )

    seed("alice")
    seed("bob")
    r = world.client.delete("/v1/admin/users/alice", headers=ROOT)
    assert r.status_code == 200
    assert r.json()["rows_removed"] >= 3
    assert deleted_blobs == ["blob-alice"]
    with engine.begin() as c:
        for name in (
            "artifacts",
            "artifact_publications",
            "artifact_views",
        ):
            assert len(c.execute(select(tables[name])).all()) == 1, name  # bob's only
        owners = {row.user_id for row in c.execute(select(tables["artifact_publications"]))}
    assert owners == {"bob"}
