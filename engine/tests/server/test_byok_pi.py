"""pi through the engine model proxy: per-provider wiring, hosted template, proxy pass-through."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.harnesses.pi_executor import _build_models_json, _only_configured_family
from omnigent.models import model_catalog
from omnigent.onboarding.provider_config import load_providers
from omnigent.runtime.workflow import _apply_provider_to_pi
from omnigent.server.hosts import parse_host_section
from omnigent.server.inference_catalog import SandboxInferenceService
from omnigent.superchat.models.proxy import create_model_proxy_router
from omnigent.superchat.models.store import ModelConnectionStore
from omnigent.superchat.vault.store import VAULT_KEY_ENV

TEMPLATE = Path(__file__).resolve().parents[3] / "deployment/engine/config.byok.yaml.tmpl"
AUTH = "python3 -m omnigent.host.model_credential token"


#: What each provider's model listing answers: the template names no model, so the catalog is
#: the connection's own list.
LISTINGS = {
    "api.anthropic.com": ["claude-sonnet-5-5", "claude-haiku-4-5"],
    "openrouter.ai": ["deepseek/deepseek-chat", "qwen/qwen3-coder", "anthropic/claude-sonnet-4.5"],
}


def _listing() -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        ids = LISTINGS.get(request.url.host or "", [])
        return httpx.Response(200, json={"data": [{"id": m, "type": "model"} for m in ids]})

    return httpx.MockTransport(respond)


def _service(state: SimpleNamespace) -> SandboxInferenceService:
    return SandboxInferenceService(state, transport=_listing())


def _hosted_state(creds: dict[str, tuple[str, str]]) -> SimpleNamespace:
    raw = yaml.safe_load(
        TEMPLATE.read_text().replace("__ENGINE_PUBLIC_URL__", "https://eng.example")
    )
    deployment = parse_host_section(raw["hosts"])
    assert deployment is not None

    class _Store:
        def get_plaintext(self, scope, owner_id, preferred):
            found = creds.get(owner_id) if scope == "user" else None
            return found if found and found[0] in preferred else None

    model_catalog.clear_model_catalog_cache()
    state = SimpleNamespace(
        sandbox_config=deployment,
        databricks_store=None,
        databricks_client=None,
        model_connection_store=_Store(),
    )
    state.inference_catalog = _service(state)
    return state


def _pi_models_json(snapshot: dict, model: str) -> tuple[dict, dict[str, str]]:
    """The models.json pi would get, built from the snapshot the way the runner wires it."""
    entry = load_providers(snapshot["runtime_config"])["byok"]
    env: dict[str, str] = {"HARNESS_PI_MODEL": model}
    _apply_provider_to_pi(env, entry)
    base_urls = json.loads(env["HARNESS_PI_GATEWAY_BASE_URLS"])
    config = _build_models_json(
        env["HARNESS_PI_GATEWAY_HOST"],
        "TOKEN",
        base_urls,
        model,
        openai_wire_api=env.get("HARNESS_PI_GATEWAY_OPENAI_WIRE_API"),
    )
    return config, env


@pytest.mark.asyncio
async def test_hosted_template_parses_and_pi_on_anthropic_is_ready() -> None:
    state = _hosted_state({"alice": ("anthropic", "sk-ant-x")})
    snapshot = await _service(state).prepare("docker", "pi", "alice")
    assert snapshot is not None and snapshot["catalog"]["status"] == "ready"
    assert snapshot["catalog"]["default_model"] == "claude-sonnet-5-5"
    config, env = _pi_models_json(snapshot, "claude-sonnet-5-5")
    assert env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] == AUTH
    provider = config["providers"]["databricks-anthropic"]
    assert provider["baseUrl"] == "https://eng.example/v1/model/anthropic"
    assert provider["api"] == "anthropic-messages" and provider["authHeader"] is True
    assert [m["id"] for m in provider["models"]] == ["claude-sonnet-5-5"]
    assert _only_configured_family(json.loads(env["HARNESS_PI_GATEWAY_BASE_URLS"])) == "claude"


@pytest.mark.asyncio
async def test_pi_on_openrouter_uses_chat_completions_through_the_proxy() -> None:
    state = _hosted_state({"bob": ("openrouter", "sk-or-x")})
    snapshot = await _service(state).prepare("docker", "pi", "bob")
    assert snapshot is not None and snapshot["catalog"]["status"] == "ready"
    ids = [m["id"] for m in snapshot["catalog"]["models"]]
    assert "deepseek/deepseek-chat" in ids and "anthropic/claude-sonnet-4.5" in ids
    for model in ("deepseek/deepseek-chat", "anthropic/claude-sonnet-4.5"):
        config, env = _pi_models_json(snapshot, model)
        assert env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] == AUTH
        assert env["HARNESS_PI_GATEWAY_OPENAI_WIRE_API"] == "chat"
        provider = config["providers"]["databricks-completions"]
        assert provider["baseUrl"] == "https://eng.example/v1/model/openrouter/v1"
        assert provider["api"] == "openai-completions" and provider["authHeader"] is True
        anthropic = config["providers"]["databricks-anthropic"]
        assert anthropic["baseUrl"] == "https://eng.example/v1/model/openrouter"
        assert anthropic["api"] == "anthropic-messages"
        claude = model.startswith("anthropic/")
        # Claude ids ride OpenRouter's Anthropic-compatible Messages API; every other model
        # rides Chat Completions.
        routed = config["providers"][
            "databricks-anthropic" if claude else "databricks-completions"
        ]
        assert [m["id"] for m in routed["models"]] == [model]


@pytest.mark.asyncio
async def test_claude_sdk_binding_is_unchanged_on_openrouter() -> None:
    state = _hosted_state({"bob": ("openrouter", "sk-or-x")})
    snapshot = await _service(state).prepare("docker", "claude-sdk", "bob")
    assert snapshot is not None
    family = snapshot["runtime_config"]["providers"]["byok"]["anthropic"]
    assert family["base_url"] == "https://eng.example/v1/model/openrouter"
    assert snapshot["runtime_config"]["providers"]["byok"]["openai"]["wire_api"] == "chat"


@pytest.mark.asyncio
async def test_pi_without_a_key_needs_one() -> None:
    from omnigent.errors import OmnigentError

    with pytest.raises(OmnigentError) as caught:
        await _service(_hosted_state({})).prepare("docker", "pi", "carol")
    assert str(caught.value.code) == "model_key_required"


def test_hosted_engine_ships_both_bundles() -> None:
    start = TEMPLATE.with_name("start.sh").read_text()
    assert "$bundle/agents/superchat-pi" in start
    assert "$bundle/agents/superchat-claude" in start


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelConnectionStore:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    uri = f"sqlite:///{tmp_path / 'p.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(uri))
    s = ModelConnectionStore(uri)
    s.put("user", "bob", "openrouter", "sk-or-v1-BOB")
    return s


def test_proxy_forwards_an_openai_chat_completion_with_bearer_injected(store) -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": []})

    class _Hosts:
        def resolve_launch_token(self, host_id, token):
            return (
                SimpleNamespace(user_id="bob") if (host_id, token) == ("host-b", "tok-b") else None
            )

    app = FastAPI()
    app.state.model_connection_store = store
    app.include_router(
        create_model_proxy_router(_Hosts(), transport=httpx.MockTransport(respond)),  # type: ignore[arg-type]
        prefix="/v1",
    )
    body = {"model": "deepseek/deepseek-chat", "messages": [], "stream": True}
    r = TestClient(app).post(
        "/v1/model/openrouter/v1/chat/completions",
        json=body,
        headers={"authorization": "Bearer host-b:tok-b"},
    )
    assert r.status_code == 200
    (req,) = seen
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-or-v1-BOB"
    # The proxy asks OpenRouter to report the charge; nothing else changes.
    assert json.loads(req.read()) == {**body, "usage": {"include": True}}
