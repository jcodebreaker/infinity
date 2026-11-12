"""Monthly model budgets: settings routes, validation, admin gating and the usage month window."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.server.routes import usage as usage_module
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.models import budget as budget_module
from omnigent.superchat.models.budget import Budget, ModelBudgets, ModelBudgetStore, month_start
from omnigent.superchat.models.budget_routes import create_budget_router

from .test_model_connection import _Auth, _Perms

ME = "/v1/me/budget"
ADMIN = "/v1/admin/budget"
ROOT = {"x-user": "root"}
# 2026-10-14 12:26 UTC
NOW = 1_792_000_000


@pytest.fixture
def uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    u = f"sqlite:///{tmp_path / 'b.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(u))
    monkeypatch.setattr(budget_module, "now_epoch", lambda: NOW)
    return u


@pytest.fixture
def ledger(uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(uri)


@pytest.fixture
def client(uri: str, ledger) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_budget_router(
            ModelBudgets(ModelBudgetStore(uri), ledger),
            auth_provider=_Auth(),  # type: ignore[arg-type]
            permission_store=_Perms(),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            {"code": str(exc.code), "message": str(exc)}, status_code=exc.http_status
        )

    return TestClient(app)


def test_default_budget_has_no_limit_and_asks(client: TestClient) -> None:
    body = client.get(ME).json()
    assert body == {
        "monthly_limit_usd": None,
        "at_limit": "ask",
        "spent_month_usd": 0.0,
        "period_start": "2026-10-01",
        "org_limit_usd": None,
        "org_at_limit": None,
    }


def test_put_round_trips_and_reports_month_spend(client: TestClient, ledger) -> None:
    ledger.add_daily_cost("u1", "2026-10-02", 1.5)
    ledger.add_daily_cost("u1", "2026-09-30", 40.0)  # last month: not counted
    put = client.put(ME, json={"monthly_limit_usd": 25, "at_limit": "stop"})
    assert put.status_code == 200
    assert put.json()["monthly_limit_usd"] == 25.0 and put.json()["at_limit"] == "stop"
    assert put.json()["spent_month_usd"] == pytest.approx(1.5)
    assert client.get(ME).json()["monthly_limit_usd"] == 25.0
    # at_limit defaults to ask; null removes the limit
    assert client.put(ME, json={"monthly_limit_usd": 10}).json()["at_limit"] == "ask"
    cleared = client.put(ME, json={"monthly_limit_usd": None}).json()
    assert cleared["monthly_limit_usd"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"monthly_limit_usd": 0},
        {"monthly_limit_usd": -5},
        {"monthly_limit_usd": "ten"},
        {"monthly_limit_usd": True},
        {"monthly_limit_usd": 10, "at_limit": "explode"},
        {"monthly_limit_usd": 10, "extra": 1},
    ],
)
def test_invalid_budgets_are_rejected(client: TestClient, body: dict) -> None:
    r = client.put(ME, json=body)
    assert r.status_code in (400, 422)
    assert client.get(ME).json()["monthly_limit_usd"] is None


def test_budgets_are_per_person(client: TestClient) -> None:
    client.put(ME, json={"monthly_limit_usd": 5, "at_limit": "stop"})
    assert client.get(ME, headers={"x-user": "u2"}).json()["monthly_limit_usd"] is None


def test_admin_budget_needs_an_admin_with_a_neutral_message(client: TestClient) -> None:
    for call in (client.get(ADMIN), client.put(ADMIN, json={"monthly_limit_usd": 100})):
        assert call.status_code == 403
        assert "default policies" not in call.json()["message"]
        assert "model budget" in call.json()["message"]
    ok = client.put(ADMIN, json={"monthly_limit_usd": 100, "at_limit": "stop"}, headers=ROOT)
    assert ok.status_code == 200 and ok.json()["monthly_limit_usd"] == 100.0
    assert client.get(ADMIN, headers=ROOT).json()["at_limit"] == "stop"


def test_org_cap_is_visible_read_only_and_org_spend_is_admin_only(
    client: TestClient, ledger
) -> None:
    ledger.add_daily_cost("u1", "2026-10-03", 2.0)
    ledger.add_daily_cost("u2", "2026-10-03", 3.0)
    client.put(ADMIN, json={"monthly_limit_usd": 100, "at_limit": "stop"}, headers=ROOT)
    mine = client.get(ME).json()
    assert mine["org_limit_usd"] == 100.0 and mine["org_at_limit"] == "stop"
    assert "org_spent_month_usd" not in mine
    assert client.get(ME, headers=ROOT).json()["org_spent_month_usd"] == pytest.approx(5.0)
    assert client.get(ADMIN, headers=ROOT).json()["spent_month_usd"] == pytest.approx(5.0)
    # The person's PUT never touches the organization's cap.
    client.put(ME, json={"monthly_limit_usd": 1})
    assert client.get(ADMIN, headers=ROOT).json()["monthly_limit_usd"] == 100.0


def test_row_lives_in_preferences_and_unreadable_values_mean_no_limit(uri: str) -> None:
    from omnigent.db.db_models import SqlPreference, current_workspace_id
    from omnigent.db.utils import make_named_managed_session_maker

    store = ModelBudgetStore(uri)
    store.set("u1", Budget(7.5, "stop"))
    maker = make_named_managed_session_maker(get_or_create_engine(uri), query_name_prefix="t")
    with maker("t") as session:
        row = session.get(SqlPreference, (current_workspace_id(), "u1", "model_budget"))
        assert row is not None and '"monthly_limit_usd": 7.5' in row.value
        row.value = "not json"
    assert ModelBudgetStore(uri).get("u1") == Budget()
    store.set("u1", Budget())  # no limit, default action: row removed
    with maker("t") as session:
        assert session.get(SqlPreference, (current_workspace_id(), "u1", "model_budget")) is None


def test_usage_report_month_window(ledger, monkeypatch) -> None:
    monkeypatch.setattr(usage_module, "_utc_today", lambda: "2026-10-14")
    ledger.add_daily_cost("u1", "2026-09-30", 40.0)
    ledger.add_daily_cost("u1", "2026-10-01", 2.0)
    ledger.add_daily_cost("u1", "2026-10-14", 3.0)
    report = usage_module._build_usage_report(ledger, "u1", window="month")
    assert report.cost_month == pytest.approx(5.0)
    assert report.period_start == "2026-10-01"
    assert [(d.day, d.cost_usd) for d in report.daily_costs] == [
        ("2026-10-01", 2.0),
        ("2026-10-14", 3.0),
    ]
    default = usage_module._build_usage_report(ledger, "u1")
    assert default.cost_month == pytest.approx(5.0) and default.daily_costs == []
    assert month_start(NOW) == "2026-10-01"


def test_the_workspace_owner_is_a_reserved_identity() -> None:
    from omnigent.server.auth import _RESERVED_USERS

    assert budget_module.WORKSPACE_OWNER in _RESERVED_USERS
