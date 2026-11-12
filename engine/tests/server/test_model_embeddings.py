"""Embeddings through the model layer: whose key, what it costs, and when it is refused."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.superchat.models import embeddings as emb
from omnigent.superchat.models.budget import (
    ModelBudgets,
    ModelBudgetStore,
    bind_budgets,
    parse_budget,
)
from omnigent.superchat.models.org import SuspensionStore, bind_suspensions
from omnigent.superchat.models.routes import create_model_connection_router
from omnigent.superchat.models.store import ORG_OWNER, SCOPE_ORG, SCOPE_USER, ModelConnectionStore
from omnigent.superchat.vault.store import VAULT_KEY_ENV

USER_KEY = "or-user-key-1111"
ORG_KEY = "or-org-key-2222"
ANTHROPIC_KEY = "sk-ant-api03-ONLYCLAUDE-9999"


class _FakeLitellm:
    """Records ``embedding`` calls; answers one vector per input, out of order."""

    def __init__(self, *, cost: float | None = None, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cost, self.fail = cost, fail

    def embedding(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError(f"upstream said no to {kwargs['api_key']}")
        n = len(kwargs["input"])
        usage: dict[str, Any] = {"prompt_tokens": 10 * n}
        if self.cost is not None:
            usage["cost"] = self.cost
        data = [{"index": i, "embedding": [float(i), 1.0]} for i in reversed(range(n))]
        return {"data": data, "usage": usage}

    def cost_per_token(self, **kwargs: Any) -> tuple[float, float]:
        return kwargs["prompt_tokens"] * 0.000001, 0.0


class _Ledger:
    def __init__(self, spent: float = 0.0) -> None:
        self.spent = spent
        self.added: list[tuple[str, str, float]] = []

    def add_daily_cost(self, owner: str, day: str, usd: float) -> None:
        self.added.append((owner, day, usd))

    def sum_daily_cost(self, owner: str, since: str) -> float:
        return self.spent

    def sum_workspace_cost(self, since: str) -> float:
        return self.spent


@pytest.fixture
def uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    for operator_key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(operator_key, "operator-key-must-never-be-used")
    u = f"sqlite:///{tmp_path / 'e.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(u))
    yield u
    bind_budgets(None)
    bind_suspensions(None)
    emb.bind_embeddings(None)


def _layer(uri: str, monkeypatch: pytest.MonkeyPatch, fake: _FakeLitellm):
    monkeypatch.setattr(emb, "_load_litellm", lambda: fake)
    connections = ModelConnectionStore(uri)
    return connections, emb.ModelEmbeddings(connections, emb.EmbeddingSettingStore(uri))


def test_embeds_with_the_persons_key_base_url_and_default_model(uri, monkeypatch) -> None:
    fake = _FakeLitellm()
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    result = layer.embed("alice", ["a", "b", "c"])
    assert result.vectors == [[0.0, 1.0], [1.0, 1.0], [2.0, 1.0]]  # back in input order
    assert result.tag == "openrouter:openai/text-embedding-3-small:512"
    assert result.tokens == 30
    call = fake.calls[0]
    assert call["api_key"] == USER_KEY
    assert call["api_base"] == "https://openrouter.ai/api/v1"
    assert call["model"] == "openai/openai/text-embedding-3-small"
    assert call["dimensions"] == 512 and call["input"] == ["a", "b", "c"]


def test_the_organizations_key_is_used_when_the_person_has_none(uri, monkeypatch) -> None:
    fake = _FakeLitellm()
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_ORG, ORG_OWNER, "openrouter", ORG_KEY)
    layer.embed("alice", ["x"])
    assert fake.calls[-1]["api_key"] == ORG_KEY
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    layer.embed("alice", ["x"])
    assert fake.calls[-1]["api_key"] == USER_KEY
    layer.embed("bob", ["x"])
    assert fake.calls[-1]["api_key"] == ORG_KEY


def test_no_embedding_connection_never_falls_back_to_the_operators_key(uri, monkeypatch) -> None:
    fake = _FakeLitellm()
    connections, layer = _layer(uri, monkeypatch, fake)
    assert layer.available("alice") == emb.REASON_NO_CONNECTION
    assert layer.tag("alice") is None
    with pytest.raises(emb.EmbeddingUnavailable) as raised:
        layer.embed("alice", ["x"])
    assert raised.value.reason == emb.REASON_NO_CONNECTION
    # A Claude-only connection cannot embed either.
    connections.put(SCOPE_USER, "alice", "anthropic", ANTHROPIC_KEY)
    assert layer.available("alice") == emb.REASON_NO_CONNECTION
    assert fake.calls == []


def test_the_organization_chooses_the_model_and_size(uri, monkeypatch) -> None:
    fake = _FakeLitellm()
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    layer.settings_store().set(emb.EmbeddingSetting("openai/text-embedding-3-large", 256))
    assert layer.embed("alice", ["x"]).tag == "openrouter:openai/text-embedding-3-large:256"
    assert fake.calls[-1]["dimensions"] == 256
    layer.settings_store().set(emb.EmbeddingSetting("voyage/voyage-3", None))
    layer.embed("alice", ["x"])
    assert "dimensions" not in fake.calls[-1]
    layer.settings_store().set(None)
    assert layer.setting() == emb.EmbeddingSetting()


def test_large_inputs_go_in_batches(uri, monkeypatch) -> None:
    fake = _FakeLitellm()
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    result = layer.embed("alice", [f"t{i}" for i in range(130)])
    assert [len(c["input"]) for c in fake.calls] == [64, 64, 2]
    assert len(result.vectors) == 130


def test_the_cost_is_added_to_the_persons_daily_rollup(uri, monkeypatch) -> None:
    fake = _FakeLitellm(cost=0.0005)
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    ledger = _Ledger()
    bind_budgets(ModelBudgets(ModelBudgetStore(uri), ledger))
    result = layer.embed("alice", ["a", "b"])
    assert result.cost_usd == 0.0005
    assert [(o, c) for o, _, c in ledger.added] == [("alice", 0.0005)]


def test_without_a_reported_cost_the_price_map_prices_the_tokens(uri, monkeypatch) -> None:
    fake = _FakeLitellm()
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    ledger = _Ledger()
    bind_budgets(ModelBudgets(ModelBudgetStore(uri), ledger))
    layer.embed("alice", ["a", "b"])  # 20 tokens
    assert ledger.added[0][2] == pytest.approx(0.00002)


def test_a_used_up_stop_budget_refuses_before_any_call(uri, monkeypatch) -> None:
    fake = _FakeLitellm()
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    store = ModelBudgetStore(uri)
    store.set("alice", parse_budget(1.0, "stop"))
    bind_budgets(ModelBudgets(store, _Ledger(spent=2.0)))
    assert layer.available("alice") == emb.REASON_BUDGET
    with pytest.raises(emb.EmbeddingUnavailable) as raised:
        layer.embed("alice", ["x"])
    assert raised.value.reason == emb.REASON_BUDGET
    assert fake.calls == []


def test_a_suspended_person_cannot_embed(uri, monkeypatch) -> None:
    fake = _FakeLitellm()
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    suspensions = SuspensionStore(uri)
    suspensions.suspend("alice", "root")
    bind_suspensions(suspensions)
    assert layer.available("alice") == emb.REASON_SUSPENDED
    assert layer.available("bob") == emb.REASON_NO_CONNECTION


def test_a_provider_failure_is_reported_without_the_key(uri, monkeypatch) -> None:
    fake = _FakeLitellm(fail=True)
    connections, layer = _layer(uri, monkeypatch, fake)
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    with pytest.raises(emb.EmbeddingUnavailable) as raised:
        layer.embed("alice", ["x"])
    assert raised.value.reason == emb.REASON_ERROR
    assert USER_KEY not in str(raised.value) and raised.value.__cause__ is None


class _Auth:
    def get_user_id(self, request: Request) -> str:
        return request.headers.get("x-user", "u1")


class _Perms:
    def is_admin(self, user_id: str) -> bool:
        return user_id == "root"


def test_admin_route_reads_and_sets_the_embedding_model(uri) -> None:
    app = FastAPI()
    app.include_router(
        create_model_connection_router(
            ModelConnectionStore(uri),
            auth_provider=_Auth(),  # type: ignore[arg-type]
            permission_store=_Perms(),
            embedding_settings=emb.EmbeddingSettingStore(uri),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.http_status)

    client = TestClient(app)
    root = {"x-user": "root"}
    assert client.get("/v1/admin/embedding-model", headers={"x-user": "u1"}).status_code == 403
    assert client.get("/v1/admin/embedding-model", headers=root).json() == {
        "model": "openai/text-embedding-3-small",
        "dimensions": 512,
    }
    put = client.put(
        "/v1/admin/embedding-model",
        json={"model": "openai/text-embedding-3-large", "dimensions": 1024},
        headers=root,
    )
    assert put.json() == {"model": "openai/text-embedding-3-large", "dimensions": 1024}
    assert (
        client.put(
            "/v1/admin/embedding-model", json={"model": "m", "dimensions": 3}, headers=root
        ).status_code
        == 400
    )
    reset = client.put("/v1/admin/embedding-model", json={"model": None}, headers=root)
    assert reset.json()["model"] == "openai/text-embedding-3-small"


def test_plan_names_the_provider_and_model_a_host_embeds_with(uri, monkeypatch) -> None:
    connections, layer = _layer(uri, monkeypatch, _FakeLitellm())
    with pytest.raises(emb.EmbeddingUnavailable) as raised:
        layer.plan("alice")
    assert raised.value.reason == emb.REASON_NO_CONNECTION
    connections.put(SCOPE_USER, "alice", "openrouter", USER_KEY)
    plan = layer.plan("alice")
    assert (plan.provider, plan.model, plan.dimensions) == (
        "openrouter",
        "openai/text-embedding-3-small",
        512,
    )
    assert plan.tag == "openrouter:openai/text-embedding-3-small:512"
