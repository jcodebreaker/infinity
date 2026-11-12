"""What the model proxy charges: the stop backstop, cost read off responses, no double count."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.utils import now_epoch, utc_day
from omnigent.errors import OmnigentError
from omnigent.llms.context_window import ModelPricing
from omnigent.runtime.public_error_codes import PUBLIC_ERROR_CODES, public_error_code
from omnigent.server.inference_catalog import SandboxInferenceService, snapshot_uses_model_proxy
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.superchat.models import spend as spend_module
from omnigent.superchat.models.budget import (
    WORKSPACE_OWNER,
    Budget,
    ModelBudgets,
    ModelBudgetStore,
)
from omnigent.superchat.models.proxy import create_model_proxy_router
from omnigent.superchat.models.spend import UsageTap, cost_usd

from .test_byok_pi import _hosted_state
from .test_model_proxy import _Hosts
from .test_model_proxy import store as store

ALICE = {"x-api-key": "host-a:tok-a"}
BOB = {"authorization": "Bearer host-b:tok-b"}
RATES = ModelPricing(input_per_token=1e-6, output_per_token=5e-6)


@pytest.fixture
def ledger(tmp_path: Path, store) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'p.db'}")


@pytest.fixture
def budgets(tmp_path: Path, ledger) -> ModelBudgets:
    return ModelBudgets(ModelBudgetStore(f"sqlite:///{tmp_path / 'p.db'}"), ledger)


@pytest.fixture(autouse=True)
def _rates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        spend_module,
        "fetch_model_pricing",
        lambda model: RATES if model.startswith("claude") else None,
    )


def _client(store, budgets, upstream) -> tuple[TestClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return upstream(request)

    app = FastAPI()
    app.state.model_connection_store = store
    app.state.model_budgets = budgets
    app.include_router(
        create_model_proxy_router(_Hosts(), transport=httpx.MockTransport(respond)),  # type: ignore[arg-type]
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"code": str(exc.code)}, status_code=exc.http_status)

    return TestClient(app), seen


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def _sse(chunks: list[bytes]):
    return lambda _r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_Stream(chunks)
    )


def _today(ledger, owner: str) -> float:
    return ledger.get_daily_cost(owner, utc_day(now_epoch()))


# ── the stop backstop ──────────────────────────────────


def test_proxy_refuses_an_owner_at_a_stop_limit(store, budgets, ledger) -> None:
    client, seen = _client(store, budgets, lambda _r: httpx.Response(200, json={}))
    ledger.add_daily_cost("alice", utc_day(now_epoch()), 10.0)
    budgets.store.set("alice", Budget(10.0, "stop"))
    r = client.post("/v1/model/anthropic/v1/messages", json={"model": "claude-x"}, headers=ALICE)
    assert r.status_code == 402 and seen == []
    assert r.json()["error"]["code"] == "model_budget_exhausted"
    assert "monthly model budget" in r.json()["error"]["message"]
    # The harness classifies the provider-shaped error into the public code.
    assert public_error_code(r.json()["error"]["type"]) == "model_budget_exhausted"
    assert "model_budget_exhausted" in PUBLIC_ERROR_CODES
    # Someone else (and an "ask" limit) still gets through.
    bob = client.post("/v1/model/openrouter/v1/messages", json={"model": "m/x"}, headers=BOB)
    assert bob.status_code == 200
    budgets.store.set("alice", Budget(10.0, "ask"))
    assert (
        client.post("/v1/model/anthropic/v1/messages", json={}, headers=ALICE).status_code == 200
    )


def test_org_stop_limit_covers_every_user(store, budgets, ledger) -> None:
    client, seen = _client(store, budgets, lambda _r: httpx.Response(200, json={}))
    ledger.add_daily_cost("carol", utc_day(now_epoch()), 99.0)
    budgets.store.set(WORKSPACE_OWNER, Budget(100.0, "stop"))
    assert client.post("/v1/model/openrouter/v1/messages", json={}, headers=BOB).status_code == 200
    ledger.add_daily_cost("carol", utc_day(now_epoch()), 1.0)
    r = client.post("/v1/model/openrouter/v1/messages", json={}, headers=BOB)
    assert r.status_code == 402 and "organization" in r.json()["error"]["message"]
    assert len(seen) == 1


# ── cost capture ───────────────────────────────────────


def test_anthropic_cost_comes_from_tokens_and_catalog_rates(store, budgets, ledger) -> None:
    body = {"model": "claude-sonnet-5-5", "usage": {"input_tokens": 1000, "output_tokens": 200}}
    client, _ = _client(store, budgets, lambda _r: httpx.Response(200, json=body))
    r = client.post(
        "/v1/model/anthropic/v1/messages", json={"model": "claude-sonnet-5-5"}, headers=ALICE
    )
    assert r.json() == body
    assert _today(ledger, "alice") == pytest.approx(1000 * 1e-6 + 200 * 5e-6)


def test_anthropic_stream_is_passed_through_and_priced_from_message_events(
    store,
    budgets,
    ledger,
) -> None:
    chunks = [
        b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-x",'
        b'"usage":{"input_tokens":400,"cache_read_input_tokens":0,"output_tokens":1}}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
        # an event split across two chunks
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_t',
        b'okens":100}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    client, _ = _client(store, budgets, _sse(chunks))
    r = client.post("/v1/model/anthropic/v1/messages", json={"model": "claude-x"}, headers=ALICE)
    assert r.content == b"".join(chunks)
    assert _today(ledger, "alice") == pytest.approx(400 * 1e-6 + 100 * 5e-6)


def test_openrouter_json_cost_is_read_and_usage_is_requested(store, budgets, ledger) -> None:
    body = {
        "model": "deepseek/deepseek-chat",
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "cost": 0.000321},
    }
    client, seen = _client(store, budgets, lambda _r: httpx.Response(200, json=body))
    r = client.post(
        "/v1/model/openrouter/v1/chat/completions",
        json={"model": "deepseek/deepseek-chat", "messages": []},
        headers=BOB,
    )
    assert r.json() == body
    assert json.loads(seen[0].read())["usage"] == {"include": True}
    assert _today(ledger, "bob") == pytest.approx(0.000321)


def test_openrouter_stream_cost_from_final_chunk_with_chunks_untouched(
    store,
    budgets,
    ledger,
) -> None:
    chunks = [
        b'data: {"id":"1","choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"id":"1","choices":[{"delta":{"content":"llo"}}]}\n\n',
        b'data: {"id":"1","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,'
        b'"cost":0.0042}}\n\n',
        b"data: [DONE]\n\n",
    ]
    client, _ = _client(store, budgets, _sse(chunks))
    with client.stream(
        "POST",
        "/v1/model/openrouter/v1/chat/completions",
        json={"model": "deepseek/deepseek-chat", "stream": True},
        headers=BOB,
    ) as response:
        received = list(response.iter_raw())
    assert b"".join(received) == b"".join(chunks)
    assert _today(ledger, "bob") == pytest.approx(0.0042)


def test_unpriced_or_failed_calls_record_nothing(store, budgets, ledger) -> None:
    unknown = {"model": "mystery/model", "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
    client, _ = _client(store, budgets, lambda _r: httpx.Response(200, json=unknown))
    client.post("/v1/model/openrouter/v1/messages", json={"model": "mystery/model"}, headers=BOB)
    failing, _ = _client(
        store, budgets, lambda _r: httpx.Response(500, json={"usage": {"cost": 9}})
    )
    failing.post("/v1/model/openrouter/v1/chat/completions", json={}, headers=BOB)
    assert _today(ledger, "bob") == 0.0


def test_tap_ignores_garbage_and_oversized_lines() -> None:
    tap = UsageTap("text/event-stream")
    tap.feed(b'data: {not json "usage"\n\n')
    tap.feed(b"x" * (2 * 1024 * 1024))
    tap.feed(b'\ndata: {"usage":{"input_tokens":3,"output_tokens":4},"model":"claude-a"}\n')
    usage = tap.finish()
    assert usage is not None and (usage.input, usage.output, usage.model) == (3, 4, "claude-a")
    assert UsageTap("application/json").finish() is None
    assert cost_usd(usage) == pytest.approx(3e-6 + 20e-6)


# ── one turn, one increment ────────────────────────────


@pytest.mark.asyncio
async def test_a_proxied_turn_is_counted_by_the_proxy_only(
    store,
    budgets,
    ledger,
    tmp_path: Path,
) -> None:
    from omnigent.server.routes._sessions.orchestration import _accumulate_session_usage

    proxied = await SandboxInferenceService(
        _hosted_state({"alice": ("anthropic", "sk-ant-x")})
    ).prepare("docker", "claude-sdk", "alice")
    assert snapshot_uses_model_proxy(proxied)
    assert not snapshot_uses_model_proxy(None)
    other = json.loads(json.dumps(proxied))
    other["runtime_config"]["providers"]["byok"]["anthropic"]["auth_command"] = "other"
    assert not snapshot_uses_model_proxy(other)

    perms = SqlAlchemyPermissionStore(f"sqlite:///{tmp_path / 'p.db'}")

    def session(owner: str, snapshot) -> str:
        conv = ledger.create_conversation(inference_snapshot=snapshot)
        perms.ensure_user(owner)
        perms.grant(owner, conv.id, 4)
        return conv.id

    turn = {"usage": {"input_tokens": 1000, "output_tokens": 200, "model": "m", "cost_usd": 0.004}}
    client, _ = _client(
        store,
        budgets,
        lambda _r: httpx.Response(
            200, json={"model": "claude-x", "usage": {"input_tokens": 1000, "output_tokens": 200}}
        ),
    )
    # The turn goes through the proxy (records its price) and then the relay reports it.
    client.post("/v1/model/anthropic/v1/messages", json={"model": "claude-x"}, headers=ALICE)
    through_proxy = _today(ledger, "alice")
    assert through_proxy == pytest.approx(1000 * 1e-6 + 200 * 5e-6)
    _accumulate_session_usage(turn, session("alice", proxied), ledger)
    assert _today(ledger, "alice") == pytest.approx(through_proxy)  # unchanged: one increment

    # A session that does not use the proxy is still recorded by the relay, as before.
    _accumulate_session_usage(turn, session("dave", None), ledger)
    assert _today(ledger, "dave") == pytest.approx(0.004)
    _accumulate_session_usage(turn, session("erin", other), ledger)
    assert _today(ledger, "erin") == pytest.approx(0.004)
