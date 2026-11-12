"""Background work's model: the owner's Light model over the owner's connection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnigent.llms.background import (
    provider_connection,
    register_background_llm,
    resolve_background_llm,
)
from omnigent.onboarding.provider_config import load_providers
from omnigent.server.hosts import parse_host_section
from omnigent.superchat.models import tiers
from omnigent.superchat.models.background import BackgroundModels, background_harness
from omnigent.superchat.models.selection import ModelPicks

from .test_byok_pi import _hosted_state, _service

DEFAULT_TEMPLATE = Path(__file__).resolve().parents[3] / "deployment/engine/config.yaml.tmpl"


def _with_picks(state: SimpleNamespace, light: dict, defaults: dict | None = None):
    picks = ModelPicks(foundation=defaults or {}, light=light)
    state.model_preference_store = SimpleNamespace(read=lambda _owner: picks)
    return state


def test_the_harness_is_the_one_with_a_light_pick_else_a_foundation_else_the_first() -> None:
    bound = ["claude-sdk", "pi"]
    assert background_harness(bound, ModelPicks()) == "claude-sdk"
    assert background_harness(bound, ModelPicks(foundation={"pi": "x"})) == "pi"
    assert background_harness(
        bound, ModelPicks(foundation={"pi": "x"}, light={"claude-sdk": "y"})
    ) == ("claude-sdk")
    assert background_harness([], ModelPicks()) is None


@pytest.mark.asyncio
async def test_the_persons_light_pick_runs_on_their_anthropic_key() -> None:
    state = _with_picks(
        _hosted_state({"alice": ("anthropic", "sk-ant-x")}), {"claude-sdk": "claude-haiku-4-5"}
    )
    resolved = await BackgroundModels(state).resolve("alice")
    assert resolved == (
        "anthropic/claude-haiku-4-5",
        {"api_key": "sk-ant-x", "base_url": "https://api.anthropic.com/v1"},
    )
    assert await BackgroundModels(state).light_model("alice") == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_nothing_picked_runs_on_the_cheapest_model_by_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices = {"claude-sonnet-5-5": (3e-6, 15e-6), "claude-haiku-4-5": (1e-6, 5e-6)}
    monkeypatch.setattr(tiers, "catalog_price", prices.get)
    state = _with_picks(_hosted_state({"alice": ("anthropic", "sk-ant-x")}), {})
    model, _ = await BackgroundModels(state).resolve("alice") or ("", None)
    assert model == "anthropic/claude-haiku-4-5"


@pytest.mark.asyncio
async def test_nothing_picked_and_nothing_priced_uses_the_catalog_default() -> None:
    state = _with_picks(_hosted_state({"alice": ("anthropic", "sk-ant-x")}), {})
    model, _ = await BackgroundModels(state).resolve("alice") or ("", None)
    assert model == "anthropic/claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_a_bearer_upstream_is_called_over_chat_completions() -> None:
    state = _with_picks(_hosted_state({"bob": ("openrouter", "sk-or-x")}), {})
    resolved = await BackgroundModels(state).resolve("bob")
    assert resolved == (
        "openai_compatible/deepseek/deepseek-chat",
        {"api_key": "sk-or-x", "base_url": "https://openrouter.ai/api/v1"},
    )


@pytest.mark.asyncio
async def test_no_key_means_nothing_to_call_not_an_error() -> None:
    state = _with_picks(_hosted_state({}), {"claude-sdk": "claude-haiku-4-5"})
    assert await BackgroundModels(state).resolve("carol") is None


@pytest.mark.asyncio
async def test_an_operator_gateway_is_called_through_its_own_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator")
    raw = yaml.safe_load(
        DEFAULT_TEMPLATE.read_text().replace("__ENGINE_PUBLIC_URL__", "https://eng.example")
    )
    deployment = parse_host_section(raw["hosts"])
    state = SimpleNamespace(
        sandbox_config=deployment,
        databricks_store=None,
        databricks_client=None,
        model_connection_store=None,
    )
    _with_picks(state, {"claude-sdk": "claude-haiku-4-5"})
    state.inference_catalog = _service(state)
    resolved = await BackgroundModels(state).resolve(None)
    assert resolved == (
        "anthropic/claude-haiku-4-5",
        {"base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant-operator"},
    )


def test_provider_connection_takes_the_wire_from_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GW_KEY", "k")
    providers = load_providers(
        {
            "providers": {
                "messages": {
                    "kind": "gateway",
                    "anthropic": {"base_url": "https://gw.example", "api_key_ref": "env:GW_KEY"},
                },
                "chat": {
                    "kind": "gateway",
                    "openai": {
                        "base_url": "https://gw.example/v1",
                        "api_key_ref": "env:GW_KEY",
                        "wire_api": "chat",
                    },
                },
            }
        }
    )
    assert provider_connection(providers["messages"]) == (
        "anthropic",
        {"base_url": "https://gw.example/v1", "api_key": "k"},
    )
    assert provider_connection(providers["chat"]) == (
        "openai_compatible",
        {"base_url": "https://gw.example/v1", "api_key": "k"},
    )


@pytest.mark.asyncio
async def test_the_registered_resolver_answers_when_no_server_llm_is_configured() -> None:
    async def resolver(owner: str | None) -> tuple[str, dict[str, str] | None]:
        return f"anthropic/fast-for-{owner}", None

    register_background_llm(resolver)
    try:
        assert await resolve_background_llm("alice") == ("anthropic/fast-for-alice", None)
    finally:
        register_background_llm(None)
    assert await resolve_background_llm("alice") is None
