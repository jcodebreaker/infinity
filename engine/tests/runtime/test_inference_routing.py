"""Per-harness inference bindings survive snapshots and do not activate other gateways."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.host.connect import _build_runner_env
from omnigent.inference_config import inference_config_scope
from omnigent.models.model_catalog import (
    list_models_for_worker,
)
from omnigent.runtime.workflow import (
    _build_claude_sdk_spawn_env,
    _resolve_provider_for_build,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIGENT_HARNESS_TMP_PARENT", str(tmp_path / "harness"))
    monkeypatch.delenv("OMNIGENT_INFERENCE_CONFIG", raising=False)
    monkeypatch.setenv("BIFROST_TEST_KEY", "test-bifrost-token")
    monkeypatch.setenv("UNITY_TEST_KEY", "test-unity-token")


def _profile() -> dict[str, object]:
    providers = {}
    for name in ("bifrost", "unity"):
        providers[name] = {
            "kind": "gateway",
            "anthropic": {
                "base_url": f"https://{name}.example/anthropic",
                "api_key_ref": f"env:{name.upper()}_TEST_KEY",
                "models": {"default": "model-a"},
            },
            "openai": {
                "base_url": f"https://{name}.example/v1",
                "api_key_ref": f"env:{name.upper()}_TEST_KEY",
                "models": {"default": "model-a"},
            },
        }
    harnesses = {
        harness: {
            "provider": "unity" if harness == "claude-native" else "bifrost",
            "default_model": "model-a",
            "model_allowlist": ["model-a", "databricks-literal/model-b"],
        }
        for harness in (
            "claude-native",
            "claude-sdk",
            "codex-native",
            "pi-native",
        )
    }
    return {"providers": providers, "inference": {"harnesses": harnesses}}


def _spec(harness: str, model: str | None = None, provider: str | None = None) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="inference-test",
        instructions="Test routing.",
        executor=ExecutorSpec(
            type="omnigent",
            model=model,
            config={"harness": harness},
            auth=ProviderAuth(name=provider) if provider else None,
        ),
    )


@pytest.mark.parametrize("harness", ["claude-sdk", "pi"])
def test_unbound_host_keeps_its_explicit_provider_and_model(harness, tmp_path) -> None:
    from omnigent.runner.app import _HARNESS_MODEL_ENV_KEY, _build_spawn_env_from_spec

    config = _profile()
    config.pop("inference")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    spec = _spec(harness, "gpt-legacy", provider="bifrost")
    provider = _resolve_provider_for_build(spec, harness_type=harness)
    assert provider is not None and provider.name == "bifrost"
    env = _build_spawn_env_from_spec(spec, harness)
    assert env[_HARNESS_MODEL_ENV_KEY[harness]] == "gpt-legacy"
    assert "HARNESS_PI_PRESERVE_MODEL_IDS" not in env


@pytest.mark.parametrize("harness", ["claude-sdk", "pi"])
def test_bound_sdk_selected_model_precedes_an_obsolete_spec_pin(harness: str) -> None:
    from omnigent.runner.app import _HARNESS_MODEL_ENV_KEY, _build_spawn_env_from_spec

    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            harness: {
                "provider": "bifrost",
                "default_model": "model-a",
                "model_allowlist": ["model-a"],
            }
        }
    }
    with inference_config_scope(profile):
        env = _build_spawn_env_from_spec(
            _spec(harness, "obsolete-spec-model"), harness, model_override="model-a"
        )
    assert env is not None
    assert env[_HARNESS_MODEL_ENV_KEY[harness]] == "model-a"


def test_bound_claude_sdk_preserves_a_literal_anthropic_prefix() -> None:
    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            "claude-sdk": {
                "provider": "bifrost",
                "default_model": "anthropic/private-model[large]",
                "model_allowlist": ["anthropic/private-model[large]"],
            }
        }
    }
    with inference_config_scope(profile):
        env = _build_claude_sdk_spawn_env(_spec("claude-sdk"))
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "anthropic/private-model[large]"


@pytest.mark.asyncio
async def test_bound_pi_sdk_preserves_literal_suffix_through_executor_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.harnesses.pi_harness import _build_pi_executor
    from omnigent.runtime.workflow import _build_pi_spawn_env

    profile = _profile()
    profile["inference"] = {
        "harnesses": {
            "pi": {
                "provider": "bifrost",
                "default_model": "private/model[large]",
                "model_allowlist": ["private/model[large]"],
            }
        }
    }
    with inference_config_scope(profile):
        env = _build_pi_spawn_env(_spec("pi"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("omnigent.harnesses.pi_harness.resolve_harness_path", lambda _: "/fake/pi")
    monkeypatch.setattr(
        "omnigent.harnesses.pi_executor._fetch_shell_command_token", lambda _: "token"
    )
    executor = _build_pi_executor()
    assert await executor._resolve_model(None) == "private/model[large]"
    assert env["HARNESS_PI_GATEWAY_OPENAI_WIRE_API"] == "responses"


def test_ordinary_host_keeps_legacy_credential_forwarding(monkeypatch) -> None:
    config = _profile()
    config.pop("inference")
    config["providers"]["bifrost"]["openai"]["api_key_ref"] = "$CUSTOM_GATEWAY_KEY"
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)
    env = _build_runner_env(
        {"CUSTOM_GATEWAY_KEY": "private", "UNITY_TEST_KEY": "legacy-token"},
        server_url="https://omnigent.example",
        runner_id="runner",
        binding_token="binding",
        workspace="/tmp",
        parent_pid=1,
    )
    assert "CUSTOM_GATEWAY_KEY" not in env
    assert env["UNITY_TEST_KEY"] == "legacy-token"


def test_worker_catalog_intersects_live_inventory_without_rejecting_private_aliases() -> None:
    from omnigent.models import model_catalog

    model_catalog._listing_cache.clear()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {"id": "unlisted"},
                    {"id": "databricks-literal/model-b"},
                    {"id": "model-a"},
                ]
            },
        )
    )
    with inference_config_scope(_profile()):
        listing = list_models_for_worker(_spec("claude-sdk"), "claude-sdk", transport=transport)
    assert listing.verified
    assert [row.id for row in listing.models] == ["model-a", "databricks-literal/model-b"]
