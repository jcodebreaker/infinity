"""The ``litellm`` model upstream: an operator-run LiteLLM proxy used as a model connection."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.models import model_catalog
from omnigent.superchat.models.upstreams import UPSTREAMS, preferred_providers
from tests.server.test_model_connection import ME, ROOT, _app, _Probe, uri  # noqa: F401
from tests.server.test_model_proxy import _client
from tests.server.test_model_proxy import store as proxy_store  # noqa: F401

LITELLM_KEY = "sk-litellm-SECRET-9999"
LITELLM = UPSTREAMS["litellm"]


@pytest.fixture
def litellm_url(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("LITELLM_BASE_URL", "https://llm.corp.example/v1/")
    return "https://llm.corp.example"


def test_origin_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    assert LITELLM.origin == "" and not LITELLM.configured
    for raw in ("http://litellm:4000", "http://litellm:4000/", "http://litellm:4000/v1"):
        monkeypatch.setenv("LITELLM_BASE_URL", raw)
        assert LITELLM.origin == "http://litellm:4000" and LITELLM.configured
    assert LITELLM.probe_url == "http://litellm:4000/v1/models"
    assert LITELLM.embeddings_api_base == "http://litellm:4000/v1"
    # Hosted upstreams are fixed and always offered.
    assert UPSTREAMS["openrouter"].embeddings_api_base == "https://openrouter.ai/api/v1"
    assert UPSTREAMS["anthropic"].configured


def test_registry_facts() -> None:
    assert LITELLM.auth == "bearer" and LITELLM.wire_apis == {"messages", "chat"}
    assert LITELLM.lists_models and LITELLM.operator_key_env == "LITELLM_API_KEY"
    # Its ids are the proxy's own names: passed through unchanged.
    assert LITELLM.upstream_model("claude-sonnet-team") == "claude-sonnet-team"
    assert LITELLM.key_header(LITELLM_KEY) == {"Authorization": f"Bearer {LITELLM_KEY}"}
    assert preferred_providers("gpt")[-1] == "litellm"
    assert preferred_providers("claude") == ("anthropic", "openrouter", "litellm")


def test_put_probes_the_configured_endpoint(uri: str, litellm_url: str) -> None:  # noqa: F811
    probe = _Probe()
    client = _app(uri, probe)
    r = client.put(f"{ME}/litellm", json={"api_key": LITELLM_KEY})
    assert r.status_code == 200 and r.json()["provider"] == "litellm"
    (req,) = probe.requests
    assert str(req.url) == f"{litellm_url}/v1/models"
    assert req.headers["authorization"] == f"Bearer {LITELLM_KEY}"
    assert LITELLM_KEY not in client.get(ME).text


def test_org_connection_on_the_admin_route(uri: str, litellm_url: str) -> None:  # noqa: F811
    client = _app(uri, _Probe())
    r = client.put(
        "/v1/admin/model-connections/litellm", json={"api_key": LITELLM_KEY}, headers=ROOT
    )
    assert r.status_code == 200 and r.json()["scope"] == "org"


def test_unconfigured_litellm_is_not_offered(
    uri: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    probe = _Probe()
    r = _app(uri, probe).put(f"{ME}/litellm", json={"api_key": LITELLM_KEY})
    assert r.status_code == 400 and "anthropic, openrouter" in r.text and "litellm" not in r.text
    assert probe.requests == [] and LITELLM_KEY not in r.text


def test_proxy_forwards_to_the_configured_origin(proxy_store, litellm_url: str) -> None:  # noqa: F811
    proxy_store.put("user", "alice", "litellm", LITELLM_KEY)
    for path in ("v1/messages", "v1/chat/completions"):
        seen: list[httpx.Request] = []
        r = _client(proxy_store, seen).post(
            f"/v1/model/litellm/{path}",
            json={"model": "gpt-team"},
            headers={"x-api-key": "host-a:tok-a"},
        )
        assert r.status_code == 200
        (req,) = seen
        assert str(req.url) == f"{litellm_url}/{path}"
        assert req.headers["authorization"] == f"Bearer {LITELLM_KEY}"
        assert "x-api-key" not in req.headers
        assert json.loads(req.read()) == {"model": "gpt-team"}


def test_proxy_refuses_an_unconfigured_litellm(proxy_store, monkeypatch) -> None:  # noqa: F811
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    proxy_store.put("user", "alice", "litellm", LITELLM_KEY)
    seen: list[httpx.Request] = []
    r = _client(proxy_store, seen).post(
        "/v1/model/litellm/v1/messages", json={}, headers={"x-api-key": "host-a:tok-a"}
    )
    assert r.status_code == 404 and seen == []


def _listing(models: list[str], seen: list[httpx.Request]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in models]})

    return httpx.MockTransport(respond)


def _litellm_state(monkeypatch: pytest.MonkeyPatch, *, harness: str):
    from tests.server.test_inference_catalog import _byok_state

    state = _byok_state({"alice": ("litellm", LITELLM_KEY)})
    config = state.sandbox_config.default.host_config
    # A LiteLLM deployment names its own models: no static allowlist or default.
    config["providers"]["byok"]["anthropic"] = {}
    config["inference"]["harnesses"] = {harness: {"provider": "byok"}}
    model_catalog.clear_model_catalog_cache()
    return state


@pytest.mark.asyncio
async def test_catalog_lists_the_endpoint_models_for_pi(monkeypatch, litellm_url: str) -> None:
    from omnigent.server.inference_catalog import SandboxInferenceService

    seen: list[httpx.Request] = []
    state = _litellm_state(monkeypatch, harness="pi")
    service = SandboxInferenceService(state, transport=_listing(["gpt-team", "claude-team"], seen))
    snapshot = await service.prepare("docker", "pi", "alice")
    assert snapshot is not None and snapshot["catalog"]["status"] == "ready"
    assert [row["id"] for row in snapshot["catalog"]["models"]] == ["gpt-team", "claude-team"]
    assert str(seen[0].url) == f"{litellm_url}/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {LITELLM_KEY}"
    byok = snapshot["runtime_config"]["providers"]["byok"]
    proxy = "https://engine.example/v1/model/litellm"
    assert byok["anthropic"]["base_url"] == proxy
    assert byok["openai"] == {
        "base_url": f"{proxy}/v1",
        "wire_api": "chat",
        "auth_command": "python3 -m omnigent.host.model_credential token",
    }
    assert LITELLM_KEY not in json.dumps(snapshot)


def _listing_with_info(models: list[str], info: list[dict]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": info})
        return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in models]})

    return httpx.MockTransport(respond)


@pytest.mark.asyncio
async def test_catalog_offers_claude_sdk_every_chat_model_whatever_its_name(
    monkeypatch, litellm_url: str
) -> None:
    from omnigent.server.inference_catalog import SandboxInferenceService
    from omnigent.superchat.models.listing import clear_model_info_cache

    clear_model_info_cache()
    state = _litellm_state(monkeypatch, harness="claude-sdk")
    info = [
        {"model_name": "gpt-team", "model_info": {"mode": "chat"}},
        {"model_name": "embed-team", "model_info": {"mode": "embedding"}},
        {
            "model_name": "no-tools",
            "model_info": {"mode": "chat", "supports_function_calling": False},
        },
    ]
    transport = _listing_with_info(["gpt-team", "claude-team", "embed-team", "no-tools"], info)
    snapshot = await SandboxInferenceService(state, transport=transport).prepare(
        "docker", "claude-sdk", "alice"
    )
    assert snapshot is not None and snapshot["catalog"]["status"] == "ready"
    # Fit is the proxy's wire (LiteLLM serves /v1/messages for any model) and what it reports
    # about each model, never the name: the embedding and tool-less models are left out.
    assert [row["id"] for row in snapshot["catalog"]["models"]] == ["gpt-team", "claude-team"]
    assert snapshot["catalog"]["default_model"] == "gpt-team"
    # The saved binding keeps the non-Claude default: the runner parses it without a name rule.
    from omnigent.inference_config import parse_inference_config

    assert parse_inference_config(snapshot["runtime_config"])["claude-sdk"].default_model == (
        "gpt-team"
    )


@pytest.mark.asyncio
async def test_the_prices_litellm_reports_choose_the_light_model(
    monkeypatch, litellm_url: str
) -> None:
    from omnigent.server.inference_catalog import SandboxInferenceService
    from omnigent.superchat.models.listing import clear_model_info_cache
    from omnigent.superchat.models.tiers import person_models_from_catalog

    clear_model_info_cache()
    state = _litellm_state(monkeypatch, harness="pi")
    info = [
        {
            "model_name": "team-big",
            "model_info": {"input_cost_per_token": 3e-6, "output_cost_per_token": 1.5e-5},
        },
        {
            "model_name": "team-small",
            "model_info": {"input_cost_per_token": 2e-7, "output_cost_per_token": 8e-7},
        },
    ]
    transport = _listing_with_info(["team-big", "team-small"], info)
    snapshot = await SandboxInferenceService(state, transport=transport).prepare(
        "docker", "pi", "alice"
    )
    assert snapshot is not None
    rows = {row["id"]: row for row in snapshot["catalog"]["models"]}
    assert rows["team-small"]["costPerToken"] == {"input": 2e-7, "output": 8e-7}
    resolved = person_models_from_catalog(snapshot["catalog"], {})
    assert (resolved.foundation, resolved.light) == ("team-big", "team-small")


@pytest.mark.asyncio
async def test_the_operator_key_serves_people_without_one_and_stays_in_the_engine(
    monkeypatch, litellm_url: str
) -> None:
    from omnigent.server.inference_catalog import SandboxInferenceService
    from omnigent.superchat.models.store import SCOPE_OPERATOR, resolve_model_connection
    from omnigent.superchat.models.upstreams import offered_providers

    monkeypatch.setenv("LITELLM_API_KEY", LITELLM_KEY)
    state = _litellm_state(monkeypatch, harness="pi")
    seen: list[httpx.Request] = []
    snapshot = await SandboxInferenceService(state, transport=_listing(["team"], seen)).prepare(
        "docker", "pi", "nobody"
    )
    assert snapshot is not None and snapshot["catalog"]["status"] == "ready"
    assert seen[0].headers["authorization"] == f"Bearer {LITELLM_KEY}"
    assert LITELLM_KEY not in json.dumps(snapshot)
    found = resolve_model_connection(None, owner_id="nobody", preferred=("anthropic", "litellm"))
    assert found is not None and (found.provider, found.scope) == ("litellm", SCOPE_OPERATOR)
    # Hosted vendors never fall back to an operator key.
    assert resolve_model_connection(None, owner_id="nobody", preferred=("anthropic",)) is None
    assert {"provider": "litellm", "label": "LiteLLM", "operator_key": True} in (
        offered_providers()
    )
    assert [p["provider"] for p in offered_providers({"litellm"})] == ["litellm"]


@pytest.mark.asyncio
async def test_catalog_reports_an_unreachable_endpoint(monkeypatch, litellm_url: str) -> None:
    from omnigent.server.inference_catalog import SandboxInferenceService

    state = _litellm_state(monkeypatch, harness="pi")
    down = httpx.MockTransport(lambda request: httpx.Response(401, json={}))
    snapshot = await SandboxInferenceService(state, transport=down).prepare(
        "docker", "pi", "alice"
    )
    assert snapshot is not None and snapshot["catalog"]["status"] == "unavailable"
    assert "LiteLLM" in snapshot["catalog"]["error"]
    assert LITELLM_KEY not in json.dumps(snapshot)
