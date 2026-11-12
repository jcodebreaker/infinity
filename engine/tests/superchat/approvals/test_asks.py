"""``/v1/me/asks``: the decisions inbox (approvals, plan proposals, blocked tasks)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.objective_store.sqlalchemy_store import SqlAlchemyObjectiveStore
from omnigent.superchat.approvals.asks import parse_ask_id
from omnigent.superchat.approvals.inbox_routes import create_asks_router
from omnigent.superchat.approvals.store import SqlAlchemyApprovalStore

_SESSION = uuid.uuid4().hex


@pytest.fixture()
def stores(db_uri: str) -> tuple[SqlAlchemyObjectiveStore, SqlAlchemyApprovalStore]:
    return SqlAlchemyObjectiveStore(db_uri), SqlAlchemyApprovalStore(db_uri)


@pytest.fixture()
def client(stores) -> TestClient:
    objectives, approvals = stores
    app = FastAPI()
    app.state.approval_store = approvals
    app.include_router(
        create_asks_router(
            objective_store=objectives,
            conversation_store=None,  # type: ignore[arg-type]
            agent_store=None,  # type: ignore[arg-type]
            runner_router=None,
            permission_store=None,
            auth_provider=None,
        ),
        prefix="/v1",
    )
    return TestClient(app)


def _objective(store: SqlAlchemyObjectiveStore, *, plan: bool = True) -> str:
    objective_id = uuid.uuid4().hex
    store.create(
        objective_id,
        user_id=None,
        parent_session_id=_SESSION,
        title="Audit prep",
        description="",
        due=None,
        scheduled_task_id=None,
    )
    if plan:
        store.set_open_proposal(
            uuid.uuid4().hex, objective_id, reason="First plan", plan=[{"id": None, "title": "a"}]
        )
    return objective_id


def test_parse_ask_id() -> None:
    assert parse_ask_id("approval:abc:def") == ("approval", ["abc:def"])
    assert parse_ask_id("proposal:a:b") == ("proposal", ["a", "b"])
    assert parse_ask_id("task:a") is None
    assert parse_ask_id("nope") is None


def test_lists_proposal_and_blocked_task(client: TestClient, stores) -> None:
    objectives, _ = stores
    objective_id = _objective(objectives)
    ask = client.get("/v1/me/asks").json()["data"][0]
    assert ask["kind"] == "plan_proposal"
    assert ask["objective_id"] == objective_id
    assert ask["session_id"] == _SESSION
    assert ask["subject"]["is_first_plan"] is True
    assert [c["id"] for c in ask["choices"]] == ["accept", "dismiss"]

    assert client.post(f"/v1/me/asks/{ask['id']}/answer", json={"choice": "accept"}).json() == {
        "ok": True
    }
    assert client.get("/v1/me/asks").json()["data"] == []
    task_id = objectives.get(objective_id).tasks[0].id
    objectives.update_task(objective_id, task_id, status="blocked", note="Which vendor?")

    blocked = client.get("/v1/me/asks").json()["data"][0]
    assert blocked["kind"] == "blocked_task"
    assert blocked["subject"]["note"] == "Which vendor?"
    assert [c["id"] for c in blocked["choices"]] == ["answer"]
    with pytest.raises(OmnigentError) as err:  # needs a note
        client.post(f"/v1/me/asks/{blocked['id']}/answer", json={"choice": "answer"})
    assert err.value.code == ErrorCode.INVALID_INPUT

    client.post(f"/v1/me/asks/{blocked['id']}/answer", json={"choice": "answer", "note": "Acme"})
    task = objectives.get(objective_id).tasks[0]
    assert task.status == "pending"
    assert task.note == "Which vendor?\n\nAnswer: Acme"


def test_dismiss_alias_and_paused_goal_hidden(client: TestClient, stores) -> None:
    objectives, _ = stores
    objective_id = _objective(objectives)
    paused = _objective(objectives)
    objectives.update(paused, status="paused")
    asks = client.get("/v1/me/asks").json()["data"]
    assert [a["objective_id"] for a in asks] == [objective_id]
    client.post(f"/v1/me/asks/{asks[0]['id']}/answer", json={"choice": "keep_current"})
    assert objectives.get(objective_id).open_proposal is None
    assert objectives.get(objective_id).tasks == []


def test_lists_approval_with_choices(client: TestClient, stores) -> None:
    _, approvals = stores
    approvals.put_pending(
        "elic-1",
        _SESSION,
        user_id="local",
        category="send",
        target="",
        summary="Send email",
        amount_usd=None,
        event="{}",
    )
    ask = client.get("/v1/me/asks").json()["data"][0]
    assert ask["id"] == "approval:elic-1"
    assert ask["kind"] == "approval"
    assert ask["subject"]["summary"] == "Send email"
    assert [c["id"] for c in ask["choices"]] == ["approve_once", "deny"]  # no standing rule
    assert ask["choices"][-1]["style"] == "danger"


def test_errors(client: TestClient, stores) -> None:
    objectives, _ = stores
    objective_id = _objective(objectives)
    proposal_id = objectives.get(objective_id).open_proposal.id

    def code(ask_id: str, choice: str) -> Any:
        with pytest.raises(OmnigentError) as err:
            client.post(f"/v1/me/asks/{ask_id}/answer", json={"choice": choice})
        return err.value.code

    assert code("garbage", "accept") == ErrorCode.NOT_FOUND
    assert code(f"proposal:{objective_id}:{proposal_id}", "maybe") == ErrorCode.INVALID_INPUT
    assert code(f"proposal:{uuid.uuid4().hex}:{proposal_id}", "accept") == ErrorCode.NOT_FOUND
    assert code("approval:missing", "bogus") == ErrorCode.INVALID_INPUT


def test_ask_subjects_are_redacted(
    client: TestClient, stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "pw-asks-0123456789"
    monkeypatch.setenv("BILLING_PASSWORD", secret)
    objectives, approvals = stores
    approvals.put_pending(
        "elic-2",
        _SESSION,
        user_id="local",
        category="send",
        target="",
        summary=f"Send {secret} to Bob",
        amount_usd=None,
        event="{}",
    )
    objective_id = _objective(objectives, plan=False)
    objectives.set_open_proposal(
        uuid.uuid4().hex, objective_id, reason=f"use {secret}", plan=[{"id": None, "title": "a"}]
    )
    raw = client.get("/v1/me/asks").text
    assert secret not in raw and raw.count("[redacted]") >= 2


def test_history_lists_decisions_not_supersessions(client: TestClient, stores) -> None:
    objectives, approvals = stores
    objective_id = _objective(objectives)
    # A newer proposal replaces the first: that is not the person's decision.
    objectives.set_open_proposal(
        uuid.uuid4().hex, objective_id, reason="Better plan", plan=[{"id": None, "title": "b"}]
    )
    ask = client.get("/v1/me/asks").json()["data"][0]
    client.post(f"/v1/me/asks/{ask['id']}/answer", json={"choice": "dismiss"})
    for elicitation_id in ("e_yes", "e_gone"):
        approvals.put_pending(
            elicitation_id,
            _SESSION,
            user_id="local",
            category="tool",
            target="send_mail",
            summary=f"Send mail {elicitation_id}",
            amount_usd=None,
            event="{}",
        )
    approvals.decide("e_yes", "approved", "local")
    approvals.decide("e_gone", "expired")
    approvals.decide("e_yes", "denied", "local")  # a decision is never overwritten

    history = client.get("/v1/me/asks/history").json()["data"]
    assert sorted((row["kind"], row["decision"], row["by_you"]) for row in history) == [
        ("approval", "approved", True),
        ("plan_proposal", "dismissed", True),
    ]
    assert approvals.list_pending() == []
