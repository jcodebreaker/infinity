"""Verified sandbox catalogs and saved profiles remain scoped to owner and provider."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from omnigent.errors import OmnigentError
from omnigent.models import model_catalog
from omnigent.models.model_catalog import ModelEntry, ModelListing
from omnigent.models.model_metadata import ModelMetadata, ModelReasoningMetadata, ModelWireAPI
from omnigent.server.hosts import (
    HostDeployment,
    HostProviderConfig,
    parse_host_section,
)
from omnigent.server.inference_catalog import SandboxInferenceService


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("INFERENCE_CATALOG_KEY", "catalog-test-secret")
    monkeypatch.delenv("POD_INFERENCE_KEY", raising=False)
    with model_catalog._listing_cache_lock:
        model_catalog._listing_cache.clear()


def _state(*, allowed=("gateway/fast", "gateway/main"), default="gateway/main", harness="pi"):
    binding = {"provider": "bifrost"}
    if allowed is not None:
        binding["model_allowlist"] = list(allowed)
    if default is not None:
        binding["default_model"] = default
    config = {
        "providers": {
            "bifrost": {
                "kind": "gateway",
                "openai": {
                    "base_url": "https://inference.example/v1",
                    "api_key_ref": "env:POD_INFERENCE_KEY",
                    "wire_api": "responses",
                },
            }
        },
        "inference": {"harnesses": {harness: binding}},
    }
    target = HostProviderConfig(
        server_url="http://localhost:6767",
        launcher_factory=lambda: None,
        token_ttl_s=100,
        provider="docker",
        host_config=config,
        model_discovery={
            "bifrost": {
                "base_url": "https://catalog.example/v1",
                "api_key_ref": "env:INFERENCE_CATALOG_KEY",
            }
        },
    )
    return SimpleNamespace(
        sandbox_config=HostDeployment.single(target),
    )


def _transport(ids=("gateway/main", "gateway/fast", "gateway/noisy"), requests=None):
    def respond(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json={"data": [{"id": model} for model in ids]})

    return httpx.MockTransport(respond)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["docker"])
@pytest.mark.parametrize("inference", [None, {}, {"harnesses": {}}])
async def test_unconfigured_sandboxes_preserve_legacy_credentials(provider, inference):
    config = copy.deepcopy(_state().sandbox_config.default.host_config)
    config.pop("inference")
    if inference is not None:
        config["inference"] = inference
    family = config["providers"]["bifrost"]["openai"]
    family["api_key_ref"] = "legacy-test-key"
    family["base_url"] = "${LEGACY_GATEWAY_URL}"
    deployment = parse_host_section(
        {"provider": provider, "server_url": "https://server.example", "host_config": config}
    )
    service = SandboxInferenceService(SimpleNamespace(sandbox_config=deployment))
    assert await service.prepare(provider, "pi", "alice") is None
    assert deployment.default.host_config == config


@pytest.mark.asyncio
async def test_preview_uses_server_discovery_and_never_resolves_pod_credentials():
    state = _state()
    requests = []
    snapshot = await SandboxInferenceService(
        state, transport=_transport(requests=requests)
    ).prepare("docker", "pi", "alice")
    assert snapshot is not None
    assert requests[0].url == "https://catalog.example/v1/models"
    assert requests[0].headers["authorization"] == "Bearer catalog-test-secret"
    preview = snapshot["catalog"]
    assert preview["status"] == "ready"
    assert [row["id"] for row in preview["models"]] == ["gateway/fast", "gateway/main"]
    assert preview["models"][1]["isDefault"] is True
    assert "catalog-test-secret" not in json.dumps(snapshot)
    assert (
        snapshot["runtime_config"]["providers"]["bifrost"]["openai"]["api_key_ref"]
        == "env:POD_INFERENCE_KEY"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allowed,expected",
    [
        ((), []),
        (("gateway/main",), ["gateway/main"]),
        (None, ["gateway/main", "gateway/fast", "gateway/noisy"]),
    ],
)
async def test_empty_single_and_absent_allowlists_remain_distinct(allowed, expected):
    snapshot = await SandboxInferenceService(
        _state(allowed=allowed, default=None), transport=_transport()
    ).prepare("docker", "pi", "alice")
    assert snapshot is not None
    assert [row["id"] for row in snapshot["catalog"]["models"]] == expected
    assert snapshot["catalog"]["status"] == ("ready" if expected else "empty")


@pytest.mark.asyncio
async def test_unavailable_default_is_not_inserted_into_live_catalog():
    snapshot = await SandboxInferenceService(
        _state(), transport=_transport(("gateway/fast",))
    ).prepare("docker", "pi", "alice")
    assert snapshot is not None
    assert snapshot["catalog"]["status"] == "unavailable"
    assert snapshot["catalog"]["models"] == []
    assert "default model" in snapshot["catalog"]["error"]


@pytest.mark.asyncio
async def test_saved_profile_ignores_later_target_edits_and_credential_rotation_uses_reference(
    monkeypatch,
):
    state = _state()
    requests = []
    service = SandboxInferenceService(state, transport=_transport(requests=requests))
    snapshot = await service.prepare("docker", "pi", "alice")
    assert snapshot is not None
    original = copy.deepcopy(snapshot)
    target = state.sandbox_config.default
    target.host_config["inference"]["harnesses"]["pi"]["model_allowlist"] = ["other"]
    target.model_discovery["bifrost"]["base_url"] = "https://new.example/v1"
    monkeypatch.setenv("INFERENCE_CATALOG_KEY", "rotated-secret")
    preview = await service.catalog(snapshot)
    assert preview["status"] == "ready"
    assert requests[-1].url.host == "catalog.example"
    assert requests[-1].headers["authorization"] == "Bearer rotated-secret"
    assert snapshot == original
    assert "rotated-secret" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_discovery_failure_is_redacted_and_distinct_from_empty():
    def fail(request):
        return httpx.Response(401, json={"message": "upstream-secret"})

    snapshot = await SandboxInferenceService(
        _state(), transport=httpx.MockTransport(fail)
    ).prepare("docker", "pi", "alice")
    assert snapshot is not None
    assert snapshot["catalog"]["status"] == "unavailable"
    assert "upstream-secret" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_missing_discovery_serves_static_allowlist_without_pod_key_or_public_catalog():
    # No server discovery: fall back to the operator's curated list verbatim. The service has
    # no transport, so any attempt to list the gateway would fail -- a "ready" result proves
    # no gateway call was made and no pod credential was resolved.
    state = _state()
    state.sandbox_config.default.model_discovery.clear()
    snapshot = await SandboxInferenceService(state).prepare("docker", "pi", "alice")
    assert snapshot is not None
    catalog = snapshot["catalog"]
    assert catalog["status"] == "ready"
    assert [row["id"] for row in catalog["models"]] == ["gateway/fast", "gateway/main"]
    assert catalog["default_model"] == "gateway/main"
    assert catalog["models"][1]["isDefault"] is True
    assert "catalog-test-secret" not in json.dumps(snapshot)
    assert (
        snapshot["runtime_config"]["providers"]["bifrost"]["openai"]["api_key_ref"]
        == "env:POD_INFERENCE_KEY"
    )


@pytest.mark.asyncio
async def test_missing_discovery_without_allowlist_reports_configuration_error():
    # Without discovery AND without an allowlist there is nothing to enumerate.
    state = _state(allowed=None, default=None)
    state.sandbox_config.default.model_discovery.clear()
    snapshot = await SandboxInferenceService(state).prepare("docker", "pi", "alice")
    assert snapshot is not None
    assert snapshot["catalog"]["status"] == "unavailable"
    assert "model_allowlist" in snapshot["catalog"]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [(), ("gateway/main",)])
async def test_static_catalog_preserves_empty_and_singleton_allowlists(allowed):
    state = _state(allowed=allowed, default=None)
    state.sandbox_config.default.model_discovery.clear()
    requests = []
    snapshot = await SandboxInferenceService(
        state, transport=_transport(requests=requests)
    ).prepare("docker", "pi", "alice")
    assert requests == []
    assert [row["id"] for row in snapshot["catalog"]["models"]] == list(allowed)
    assert snapshot["catalog"]["status"] == ("ready" if allowed else "empty")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location", ["provider", "unbound_provider", "discovery", "unused_discovery"]
)
@pytest.mark.parametrize("reference", ["literal-test-token", "prefix-${TOKEN}", "env:", 42])
async def test_literal_credentials_rejected_at_startup_and_before_snapshot(location, reference):
    state = _state()
    target = state.sandbox_config.default
    if location in {"provider", "unbound_provider"}:
        name = "bifrost" if location == "provider" else "unbound"
        target.host_config["providers"][name] = copy.deepcopy(
            target.host_config["providers"]["bifrost"]
        )
        target.host_config["providers"][name]["openai"]["api_key_ref"] = reference
    else:
        name = "bifrost" if location == "discovery" else "unused"
        target.model_discovery[name] = {
            "base_url": "https://catalog.example/v1",
            "api_key_ref": reference,
        }
    with pytest.raises(ValueError, match="api_key_ref") as error:
        parse_host_section(
            {
                "provider": "docker",
                "server_url": "https://server.example",
                "host_config": target.host_config,
                "model_discovery": target.model_discovery,
            }
        )
    assert "literal-test-token" not in str(error.value)
    requests = []
    with pytest.raises(OmnigentError, match="api_key_ref") as error:
        await SandboxInferenceService(state, transport=_transport(requests=requests)).prepare(
            "docker", "pi", "alice"
        )
    assert "literal-test-token" not in str(error.value)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    ["env:POD_INFERENCE_KEY", "$POD_INFERENCE_KEY", "${POD_INFERENCE_KEY}", "keychain:pod-key"],
)
async def test_pod_references_are_validated_without_resolution(reference):
    state = _state()
    target = state.sandbox_config.default
    target.host_config["providers"]["bifrost"]["openai"]["api_key_ref"] = reference
    parsed = parse_host_section(
        {
            "provider": "docker",
            "server_url": "https://server.example",
            "host_config": target.host_config,
            "model_discovery": target.model_discovery,
        }
    )
    assert parsed is not None
    snapshot = await SandboxInferenceService(state, transport=_transport()).prepare(
        "docker", "pi", "alice"
    )
    assert snapshot["catalog"]["status"] == "ready"
    assert snapshot["runtime_config"]["providers"]["bifrost"]["openai"]["api_key_ref"] == reference


@pytest.mark.asyncio
async def test_aliases_resolve_and_cycles_fail():
    state = _state(allowed=("fast", "primary"), default="primary")
    family = state.sandbox_config.default.host_config["providers"]["bifrost"]["openai"]
    family["models"] = {"primary": "gateway/main", "fast": "gateway/fast"}
    service = SandboxInferenceService(state, transport=_transport())
    snapshot = await service.prepare("docker", "pi", "alice")
    assert snapshot is not None
    assert snapshot["catalog"]["default_model"] == "gateway/main"
    family["models"] = {"primary": "fast", "fast": "primary"}
    with pytest.raises(OmnigentError, match="cycle"):
        await service.prepare("docker", "pi", "alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["pi"])
@pytest.mark.parametrize("discovery", [False, True])
async def test_dual_family_aliases_resolve_for_live_and_static_catalogs(harness, discovery):
    state = _state(allowed=("fast", "primary", "gateway/literal"), default="fast", harness=harness)
    provider = state.sandbox_config.default.host_config["providers"]["bifrost"]
    provider["openai"]["models"] = {"fast": "nested", "nested": "gateway/gpt-fast"}
    provider["anthropic"] = {
        "base_url": "https://anthropic.example",
        "api_key_ref": "env:POD_INFERENCE_KEY",
        "models": {"primary": "gateway/claude-primary"},
    }
    if not discovery:
        state.sandbox_config.default.model_discovery.clear()
    requests = []
    expected = ["gateway/gpt-fast", "gateway/claude-primary", "gateway/literal"]
    snapshot = await SandboxInferenceService(
        state, transport=_transport(expected, requests=requests)
    ).prepare("docker", harness, "alice")
    assert snapshot is not None
    assert snapshot["catalog"]["status"] == "ready"
    assert [row["id"] for row in snapshot["catalog"]["models"]] == expected
    assert snapshot["catalog"]["default_model"] == "gateway/gpt-fast"
    assert bool(requests) is discovery
    saved = snapshot["runtime_config"]["inference"]["harnesses"][harness]
    assert saved["model_allowlist"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["pi"])
@pytest.mark.parametrize("same_target", [False, True])
async def test_duplicate_family_aliases_must_agree(harness, same_target):
    state = _state(allowed=("fast",), default="fast", harness=harness)
    provider = state.sandbox_config.default.host_config["providers"]["bifrost"]
    provider["openai"]["models"] = {"fast": "gateway/fast"}
    provider["anthropic"] = {
        "base_url": "https://anthropic.example",
        "api_key_ref": "env:POD_INFERENCE_KEY",
        "models": {"fast": "gateway/fast" if same_target else "gateway/claude-fast"},
    }
    service = SandboxInferenceService(state, transport=_transport())
    if same_target:
        snapshot = await service.prepare("docker", harness, "alice")
        assert snapshot["catalog"]["default_model"] == "gateway/fast"
    else:
        with pytest.raises(OmnigentError, match="ambiguous"):
            await service.prepare("docker", harness, "alice")


@pytest.mark.asyncio
async def test_metadata_filters_wrong_wire_but_preserves_unknown_private_aliases(monkeypatch):
    entries = (
        ModelEntry(
            "gateway/main", "other", ModelMetadata(wire_apis=frozenset({ModelWireAPI.OPENAI_CHAT}))
        ),
        ModelEntry(
            "gateway/fast",
            "other",
            ModelMetadata(reasoning=ModelReasoningMetadata(efforts=frozenset({"high", "low"}))),
        ),
    )
    monkeypatch.setattr(
        model_catalog,
        "_listing_for_provider",
        lambda *_args, **_kwargs: ModelListing("gateway", True, entries, ""),
    )
    snapshot = await SandboxInferenceService(_state(default=None)).prepare("docker", "pi", "alice")
    assert snapshot is not None
    assert snapshot["catalog"]["status"] == "ready"
    assert [row["id"] for row in snapshot["catalog"]["models"]] == ["gateway/fast"]
    assert snapshot["catalog"]["models"][0]["supportedReasoningEfforts"] == [
        {"reasoningEffort": "low"},
        {"reasoningEffort": "high"},
    ]


@pytest.mark.asyncio
async def test_unbound_harness_keeps_its_baseline_when_a_binding_is_added_later():
    from omnigent.inference_config import binding_for_harness
    from omnigent.server.hosts import deployment_with_inference_snapshot

    state = _state()
    service = SandboxInferenceService(state)
    snapshot = await service.prepare("docker", "claude-sdk", "alice")
    assert snapshot is not None
    assert snapshot["catalog"]["configured"] is False
    assert snapshot["catalog"]["status"] == "unconfigured"
    state.sandbox_config.default.host_config["inference"]["harnesses"]["claude-sdk"] = {
        "provider": "bifrost"
    }
    restored = deployment_with_inference_snapshot(state.sandbox_config, snapshot)
    assert binding_for_harness(restored.default.host_config, "claude-sdk") is None
    assert (await service.catalog(snapshot))["configured"] is False


def test_legacy_restore_does_not_adopt_new_inference_bindings():
    from omnigent.server.hosts import deployment_with_inference_snapshot

    state = _state()
    restored = deployment_with_inference_snapshot(state.sandbox_config, None)
    assert restored.default.host_config["inference"] == {}
    assert state.sandbox_config.default.host_config["inference"]["harnesses"]
    assert (
        restored.default.host_config["providers"]
        == state.sandbox_config.default.host_config["providers"]
    )


@pytest.mark.asyncio
async def test_target_without_profiles_keeps_legacy_create_contract():
    state = _state()
    del state.sandbox_config.default.host_config["inference"]
    assert await SandboxInferenceService(state).prepare("docker", "claude-sdk", "alice") is None


@pytest.mark.asyncio
async def test_all_child_bindings_are_normalized_in_saved_profile():
    state = _state()
    config = state.sandbox_config.default.host_config
    config["providers"]["bifrost"]["openai"]["models"] = {"main": "gateway/main"}
    config["inference"]["harnesses"]["pi"] = {
        "provider": "bifrost",
        "model_allowlist": ["main"],
        "default_model": "main",
    }
    snapshot = await SandboxInferenceService(state, transport=_transport()).prepare(
        "docker", "pi", "alice"
    )
    assert snapshot is not None
    saved = snapshot["runtime_config"]["inference"]["harnesses"]["pi"]
    assert saved["model_allowlist"] == ["gateway/main"]
    assert saved["default_model"] == "gateway/main"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("api_key", "inline-test-secret"), ("base_url", "https://user:secret@private.example/v1")],
)
async def test_another_saved_binding_cannot_persist_embedded_credentials(field, value):
    state = _state()
    config = state.sandbox_config.default.host_config
    family = {"base_url": "https://other.example/v1", "api_key_ref": "env:POD_INFERENCE_KEY"}
    if field == "api_key":
        family.pop("api_key_ref")
    family[field] = value
    config["providers"]["other"] = {"kind": "gateway", "openai": family}
    config["inference"]["harnesses"]["pi"] = {"provider": "other"}
    with pytest.raises(OmnigentError) as error:
        await SandboxInferenceService(state, transport=_transport()).prepare(
            "docker", "pi", "alice"
        )
    assert "inline-test-secret" not in str(error.value)
    assert "user:secret" not in str(error.value)


def _byok_state(key_by_owner):
    config = {
        "providers": {
            "byok": {
                "kind": "gateway",
                "connection": "model",
                "anthropic": {"models": {"sonnet": "claude-sonnet-4-6"}},
            }
        },
        "inference": {
            "harnesses": {
                "claude-sdk": {
                    "provider": "byok",
                    "model_allowlist": ["claude-sonnet-4-6"],
                    "default_model": "claude-sonnet-4-6",
                }
            }
        },
    }
    target = HostProviderConfig(
        server_url="https://engine.example/",
        launcher_factory=lambda: None,
        token_ttl_s=100,
        provider="docker",
        host_config=config,
    )

    class _Store:
        def get_plaintext(self, scope, owner_id, preferred):
            found = key_by_owner.get(owner_id) if scope == "user" else None
            return found if found and found[0] in preferred else None

    return SimpleNamespace(
        sandbox_config=HostDeployment.single(target),
        model_connection_store=_Store(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
async def test_model_connection_points_the_host_at_the_engine_proxy(provider):
    state = _byok_state({"alice": (provider, "sk-secret-ALICE")})
    snapshot = await SandboxInferenceService(state).prepare("docker", "claude-sdk", "alice")
    assert snapshot is not None and snapshot["catalog"]["status"] == "ready"
    family = snapshot["runtime_config"]["providers"]["byok"]["anthropic"]
    assert family["base_url"] == f"https://engine.example/v1/model/{provider}"
    assert family["auth_command"] == "python3 -m omnigent.host.model_credential token"
    assert family["models"] == {"sonnet": "claude-sonnet-4-6"}
    assert "sk-secret-ALICE" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_model_connection_without_a_key_needs_one():
    state = _byok_state({"alice": ("anthropic", "sk-secret-ALICE")})
    with pytest.raises(OmnigentError) as caught:
        await SandboxInferenceService(state).prepare("docker", "claude-sdk", "bob")
    assert str(caught.value.code) == "model_key_required"
    assert caught.value.http_status == 412
    assert "Add a model API key in Settings" in str(caught.value)
