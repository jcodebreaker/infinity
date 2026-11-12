"""Tests for omnigent.harnesses.pi_credentials (native Pi provider wiring)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.harnesses import pi_credentials as creds


@pytest.fixture(autouse=True)
def _stub_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.models.model_catalog.resolve_catalog_model",
        lambda provider_name, *, family, **kwargs: SimpleNamespace(
            model_id=f"catalog-{provider_name}-{family}-default"
        ),
    )


def test_key_provider_resolves_to_inline_family() -> None:
    """A key-kind provider with an anthropic family → inline Pi provider."""
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test-literal",
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="claude-sonnet-4-6", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.api_key == "sk-test-literal"
    assert provider.auth_header is False
    assert provider.model == "claude-sonnet-4-6"


def test_inline_gateway_leaves_unused_family_secret_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later family's unset secret is not expanded when an earlier family wins."""
    monkeypatch.delenv("OMNIGENT_TEST_UNSET_GATEWAY_KEY", raising=False)
    config = {
        "providers": {
            "mixed": {
                "kind": "gateway",
                "default": "pi",
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-ant",
                    "models": {"default": "claude-opus-4-8"},
                },
                "openai": {
                    "base_url": "https://gw.example.com/codex/v1",
                    "api_key": "$OMNIGENT_TEST_UNSET_GATEWAY_KEY",
                    "models": {"default": "system.ai.gpt-5"},
                },
            }
        }
    }

    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)

    assert provider is not None
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.api_key == "sk-ant"


def test_inline_gateway_failed_token_mint_keeps_the_provider() -> None:
    """A failing ``auth_command`` skips discovery but keeps the configured provider.

    Pi must not lose its provider (and fall to its own login) because the
    one-shot listing token could not be minted; the single-family config with
    the ``!command`` apiKey still launches, and Pi retries the command itself.
    """
    config = {
        "providers": {
            "openai-gateway": {
                "kind": "gateway",
                "default": "pi",
                "openai": {
                    "base_url": "https://gw.example.com/codex/v1",
                    "auth_command": "printf '\\xff\\xfe'",
                    "wire_api": "responses",
                    "models": {"default": "system.ai.gpt-5"},
                },
            }
        }
    }

    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)

    assert provider is not None
    assert provider.api == "openai-responses"
    assert provider.model == "system.ai.gpt-5"
    assert provider.api_key == "!printf '\\xff\\xfe'"
    assert not provider.additional_providers


def test_inline_non_databricks_gateway_stays_single_family() -> None:
    """A non-Databricks gateway keeps single-family behavior (no enumeration)."""
    config = {
        "providers": {
            "openrouter": {
                "kind": "gateway",
                "default": "pi",
                "openai": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "sk-test",
                    "wire_api": "responses",
                    "models": {"default": "openai/gpt-4o"},
                },
            }
        }
    }

    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)

    assert provider is not None
    assert provider.api == "openai-responses"
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.model == "openai/gpt-4o"
    assert not provider.additional_providers


def test_managed_picker_prefix_is_not_part_of_provider_model() -> None:
    """A managed picker value resolves its provider-local model id."""
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test-literal",
                },
            }
        }
    }

    provider = creds.resolve_pi_native_provider(
        model="omnigent/claude-opus-4-7", config_loader=lambda: config
    )

    assert provider is not None
    assert provider.model == "claude-opus-4-7"


def test_subscription_default_returns_none() -> None:
    """A subscription (CLI-login) default isn't reusable by Pi → None."""
    config = {"providers": {"claude": {"kind": "subscription", "default": True, "cli": "claude"}}}
    assert creds.resolve_pi_native_provider(config_loader=lambda: config) is None


def test_pi_native_subscription_returns_none() -> None:
    """A pi subscription as the pi-surface default returns None.

    ``kind="subscription", cli="pi"`` signals "use Pi's own native auth".
    When it is configured as the pi-surface default,
    ``resolve_pi_native_provider`` returns ``None`` so Pi reads from its
    own ``~/.pi/agent`` without an Omnigent-managed ``models.json``.
    """
    config = {
        "providers": {"pi-subscription": {"kind": "subscription", "cli": "pi", "default": "pi"}}
    }
    assert creds.resolve_pi_native_provider(config_loader=lambda: config) is None


def test_no_providers_returns_none() -> None:
    """No configured providers → None (Pi uses its own login)."""
    assert creds.resolve_pi_native_provider(config_loader=dict) is None


def test_malformed_config_returns_none() -> None:
    """A loader that raises must not break launch — resolve to None."""

    def _boom() -> dict[str, object]:
        raise RuntimeError("bad config")

    assert creds.resolve_pi_native_provider(config_loader=_boom) is None


def test_unresolvable_secret_falls_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider whose secret can't resolve → None, not a hard launch failure.

    A key-kind default whose ``api_key`` references an env var absent from the
    runner env makes ``entry.family()`` raise during resolution (not during the
    config load). The contract is "any resolution failure → fall back to Pi's
    own login", so the resolver must swallow it and return ``None`` rather than
    let the exception fail the Pi terminal launch.
    """
    monkeypatch.delenv("PI_NATIVE_AUDIT_UNSET_KEY", raising=False)
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "$PI_NATIVE_AUDIT_UNSET_KEY",
                },
            }
        }
    }
    assert creds.resolve_pi_native_provider(config_loader=lambda: config) is None


def test_to_models_config_shape() -> None:
    """The rendered models.json carries baseUrl/api/apiKey/models (+authHeader)."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://x/ai-gateway/anthropic",
        api="anthropic-messages",
        model="databricks-claude-sonnet-4-6",
        api_key="!get-token",
        auth_header=True,
    )
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]
    assert entry["baseUrl"] == "https://x/ai-gateway/anthropic"
    assert entry["api"] == "anthropic-messages"
    assert entry["apiKey"] == "!get-token"
    assert entry["authHeader"] is True
    assert entry["models"] == [{"id": "databricks-claude-sonnet-4-6", "reasoning": True}]


def test_key_provider_model_options_list_the_live_anthropic_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key/gateway provider's picker rows come from its live model listing.

    The pre-launch picker renders the resolved provider's ``models.json``;
    for key/gateway providers that config carried only the configured
    default (one row), so the picker offered exactly one model while the
    endpoint serves several. The listing must be fetched live via the
    model-catalog fetchers — the same lane the Databricks paths use — with
    the configured default still present (it launches offline).
    """
    config = {
        "providers": {
            "zai": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "base_url": "https://gw.example.com/anthropic",
                    "api_key": "sk-test-literal",
                    "models": {"default": "glm-5.3"},
                },
            }
        }
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "glm-5.3"}, {"id": "glm-5.3-flash"}, {"id": "glm-5.2"}]},
        )

    options = creds.pi_native_model_options(
        config_loader=lambda: config, transport=httpx.MockTransport(_handler)
    )

    assert {o["model"] for o in options} == {
        "omnigent/glm-5.3",
        "omnigent/glm-5.3-flash",
        "omnigent/glm-5.2",
    }


def test_key_provider_model_options_list_the_live_openai_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An openai-family key/gateway provider lists its /v1/models rows too."""
    config = {
        "providers": {
            "deepseek": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "https://gw.example.com/v1",
                    "api_key": "sk-test-literal",
                    "wire_api": "chat",
                    "models": {"default": "deepseek-flash"},
                },
            }
        }
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]},
        )

    options = creds.pi_native_model_options(
        config_loader=lambda: config, transport=httpx.MockTransport(_handler)
    )

    assert {o["model"] for o in options} == {
        "omnigent/deepseek-flash",
        "omnigent/deepseek-v4-pro",
    }


def test_key_provider_model_options_degrade_to_default_when_listing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable listing endpoint degrades to the configured default.

    The launch picker must not hard-fail when the vendor's /models endpoint
    is offline or rejects the key: the configured default model still
    launches, exactly as it did before live listing existed.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    config = {
        "providers": {
            "zai": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "base_url": "https://gw.example.com/anthropic",
                    "api_key": "sk-test-literal",
                    "models": {"default": "glm-5.3"},
                },
            }
        }
    }

    options = creds.pi_native_model_options(
        config_loader=lambda: config, transport=httpx.MockTransport(_handler)
    )

    assert {o["model"] for o in options} == {"omnigent/glm-5.3"}


def test_pi_native_model_options_lists_only_managed_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-launch choices come only from the provider built by ``omni setup``.

    The configured model is the one Pi opens without a selection, so its row
    alone is the default.
    """
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
        additional_providers={
            "omnigent-openai": {
                "baseUrl": "https://api.openai.com/v1",
                "api": "openai-responses",
                "apiKey": "sk-openai",
                "models": [{"id": "gpt-5.6-sol", "name": "GPT 5.6 Sol"}],
            }
        },
    )
    monkeypatch.setattr(creds, "resolve_pi_native_provider", lambda: provider)

    assert creds.pi_native_model_options() == [
        {
            "id": "omnigent-openai/gpt-5.6-sol",
            "model": "omnigent-openai/gpt-5.6-sol",
            "displayName": "GPT 5.6 Sol",
            "isDefault": False,
        },
        {
            "id": "omnigent/claude-sonnet-4-6",
            "model": "omnigent/claude-sonnet-4-6",
            "displayName": "claude-sonnet-4-6",
            "isDefault": True,
        },
    ]


def test_openai_chat_wire_api_resolves_to_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenAI family with wire_api: chat → openai-completions API.

    This tests the fix for the DeepInfra bug where pi-native was ignoring
    the wire_api setting and always using openai-responses. Providers like
    DeepInfra implement Chat Completions (/v1/openai/chat/completions) but
    not the Responses API (/v1/openai/responses returns 404).
    """
    # Set a fake API key in the environment for testing
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-deepinfra-key")

    config = {
        "providers": {
            "deepinfra": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.deepinfra.com/v1/openai",
                    "api_key": "$OPENAI_API_KEY",
                    "wire_api": "chat",
                    "models": {"default": "zai-org/GLM-4.7"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # wire_api: chat should resolve to openai-completions, not openai-responses
    assert provider.api == "openai-completions", (
        f"Expected openai-completions but got {provider.api} "
        f"(wire_api:chat should use chat completions API, not responses)"
    )
    assert provider.base_url == "https://api.deepinfra.com/v1/openai"
    assert provider.model == "zai-org/GLM-4.7"
    assert provider.api_key == "sk-test-deepinfra-key"  # Resolved from environment
    assert provider.auth_header is False


def test_openai_responses_wire_api_default() -> None:
    """An OpenAI family without wire_api (or wire_api: responses) → openai-responses API.

    When wire_api is not set or set to "responses", the default behavior
    should be to use the OpenAI Responses API.
    """
    config = {
        "providers": {
            "openai-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "models": {"default": "gpt-4o"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Default (no wire_api) should use openai-responses
    assert provider.api == "openai-responses"
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.model == "gpt-4o"


def test_openai_responses_wire_api_explicit() -> None:
    """An OpenAI family with wire_api: responses → openai-responses API.

    When wire_api is explicitly set to "responses", it should use the
    OpenAI Responses API.
    """
    config = {
        "providers": {
            "openai-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "wire_api": "responses",
                    "models": {"default": "gpt-4o"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Explicit wire_api: responses should use openai-responses
    assert provider.api == "openai-responses"
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.model == "gpt-4o"


def test_cli_config_provider_does_not_drive_pi() -> None:
    """A cli-config provider (e.g. a codex config) never configures pi; pi uses its own login."""
    config = {
        "providers": {
            "codex-gateway": {
                "kind": "cli-config",
                "default": True,
                "cli": "codex",
                "model_provider": "Gateway",
            },
        }
    }

    assert creds.resolve_pi_native_provider(config_loader=lambda: config) is None


def test_anthropic_family_ignores_wire_api() -> None:
    """The Anthropic family always uses anthropic-messages, ignoring wire_api.

    The wire_api setting is only meaningful for the OpenAI family.
    """
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test",
                    "wire_api": "chat",  # Should be ignored for Anthropic
                    "models": {"default": "claude-4"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Anthropic should always use anthropic-messages, not affected by wire_api
    assert provider.api == "anthropic-messages"
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.model == "claude-4"
    assert provider.api_key == "sk-test"


def test_model_override_beats_inline_family_default() -> None:
    """A session model override wins over an inline family's default model."""
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test",
                    "models": {"default": "claude-sonnet-4-6"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="claude-opus-4-7", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.model == "claude-opus-4-7"
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]
    assert entry["id"] == "claude-opus-4-7"
    # The entry now carries full metadata (input, reasoning) rather than a bare id.
    assert entry.get("reasoning") is True


def test_databricks_prefixed_override_normalized_for_inline_anthropic() -> None:
    """A ``databricks-`` override against an inline Anthropic key provider strips.

    The spec's ``executor.model`` may be a Databricks-gateway id
    (``databricks-claude-opus-4-7``). That prefix only routes through the
    Databricks AI Gateway; an inline vendor-direct provider (here a
    key-kind ``api.anthropic.com``) cannot route it. The resolver must
    mechanically strip the prefix so the rendered ``models.json`` selects the
    bare ``claude-opus-4-7`` id the endpoint understands.
    """
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test",
                    "models": {"default": "claude-sonnet-4-6"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="databricks-claude-opus-4-7", config_loader=lambda: config
    )
    assert provider is not None
    # The gateway prefix is stripped for the vendor-direct Anthropic endpoint.
    assert provider.model == "claude-opus-4-7"
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]
    assert entry["id"] == "claude-opus-4-7"
    assert entry.get("reasoning") is True


def test_databricks_prefixed_override_normalized_for_inline_openai() -> None:
    """A ``databricks-`` override against an inline OpenAI provider strips too.

    Same contract as the Anthropic case for the OpenAI family: a
    ``databricks-gpt-*`` id is a gateway spelling the vendor-direct OpenAI
    endpoint cannot route, so the prefix is stripped to the bare ``gpt-*`` id.
    Vendor-direct means key-kind; a gateway-kind provider passes the id
    through verbatim (it may front the very gateway that serves it).
    """
    config = {
        "providers": {
            "openai-direct": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "models": {"default": "gpt-4o"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="databricks-gpt-5-4", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "openai-responses"
    # The gateway prefix is stripped for the vendor-direct OpenAI endpoint.
    assert provider.model == "gpt-5-4"
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]
    assert entry["id"] == "gpt-5-4"
    # The entry now carries input metadata rather than a bare id-only dict.


def test_inline_family_passes_non_mechanical_override_through() -> None:
    """A non-mechanical override (slash-shaped) passes through unchanged.

    ``normalize_model_for_provider`` only strips mechanical
    ``databricks-claude-*``/``databricks-gpt-*`` ids; a custom inline-gateway
    id like ``zai-org/GLM-4.7`` has no gateway counterpart and must survive
    verbatim so the inline endpoint can route it.
    """
    config = {
        "providers": {
            "deepinfra": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.deepinfra.com/v1/openai",
                    "api_key": "sk-test",
                    "wire_api": "chat",
                    "models": {"default": "zai-org/GLM-4.7"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="zai-org/GLM-4.7", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.model == "zai-org/GLM-4.7"
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]
    assert entry["id"] == "zai-org/GLM-4.7"
    # The entry now carries input metadata rather than a bare id-only dict.


def test_inline_family_configured_gateway_default_survives_verbatim() -> None:
    """A configured ``databricks-`` family default is not rewritten.

    A protocol-translating proxy (a LiteLLM / AI-Gateway-shaped ``/anthropic``
    passthrough) is addressed by serving-endpoint name, so stripping the prefix
    the user configured yields an id the endpoint answers ``ENDPOINT_NOT_FOUND``
    for. Only a session override is normalized for a vendor-direct endpoint.
    """
    config = {
        "providers": {
            "translating-proxy": {
                "kind": "gateway",
                "default": ["pi"],
                "anthropic": {
                    "base_url": "http://127.0.0.1:8399/ai-gateway/anthropic",
                    "api_key": "local",
                    "models": {"default": "databricks-claude-opus-4-8"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.model == "databricks-claude-opus-4-8"
    cfg = provider.to_models_config()
    assert cfg["providers"]["omnigent"]["models"][0]["id"] == "databricks-claude-opus-4-8"


# ── Cross-family routing: a known-family model served over the other family's
#    wire must carry a routing warning; own-family and "other"-token routing
#    stays silent. ──────────────────────────────────────────────────────────


def _openai_only_gateway(default_model: str = "claude-fable-5-1") -> dict[str, object]:
    """A ``kind: gateway`` provider exposing only an openai (chat) family."""
    return {
        "providers": {
            "corp-gateway": {
                "kind": "gateway",
                "default": ["pi"],
                "openai": {
                    "base_url": "https://gw.invalid/openai",
                    "api_key": "test-gateway-key",
                    "wire_api": "chat",
                    "models": {"default": default_model},
                },
            }
        }
    }


def test_claude_override_on_openai_only_gateway_carries_routing_warning() -> None:
    """A Claude override falling through to an openai family warns.

    The fallthrough still resolves (a protocol-translating proxy may serve
    it), but a raw passthrough gateway 404s every turn, so the resolution
    must not be silent.
    """
    provider = creds.resolve_pi_native_provider(
        model="claude-fable-5-1", config_loader=lambda: _openai_only_gateway()
    )
    assert provider is not None
    assert provider.api == "openai-completions"
    assert provider.credential_warning is not None
    assert "claude-fable-5-1" in provider.credential_warning
    assert "anthropic" in provider.credential_warning


def test_claude_family_default_on_openai_only_gateway_carries_routing_warning() -> None:
    """The configured family default is warned about too, not just overrides.

    The reported journey configures ``openai.models.default`` as a Claude id
    and launches with no override; the misroute must still be flagged.
    """
    provider = creds.resolve_pi_native_provider(config_loader=lambda: _openai_only_gateway())
    assert provider is not None
    assert provider.api == "openai-completions"
    assert provider.credential_warning is not None
    assert "claude-fable-5-1" in provider.credential_warning


def test_gpt_on_anthropic_only_gateway_carries_routing_warning() -> None:
    """The inverse direction warns too: a GPT id over an Anthropic wire."""
    config = {
        "providers": {
            "proxy": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "base_url": "https://litellm.internal.example.com/anthropic",
                    "api_key": "sk-a",
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(model="gpt-5-5", config_loader=lambda: config)
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.credential_warning is not None
    assert "gpt-5-5" in provider.credential_warning


def test_own_family_routing_has_no_warning() -> None:
    """A Claude model served by an anthropic family resolves silently."""
    config = {
        "providers": {
            "corp-gateway": {
                "kind": "gateway",
                "default": ["pi"],
                "anthropic": {
                    "base_url": "https://gw.invalid/anthropic",
                    "api_key": "test-gateway-key",
                    "models": {"default": "claude-fable-5-1"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="claude-fable-5-1", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.credential_warning is None


def test_other_token_fallthrough_stays_silent() -> None:
    """An id with no known family keeps the silent LiteLLM-passthrough intent."""
    config = {
        "providers": {
            "proxy": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "base_url": "https://litellm.internal.example.com/anthropic",
                    "api_key": "sk-a",
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="gemini-3-5-flash", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.credential_warning is None


# ── Provider-qualified overrides: a ``provider/`` prefix naming a configured
#    omnigent provider selects that provider's model; any other slash id is
#    the endpoint's own model naming and stays verbatim. ────────────────────


def _rpw_fable_anthropic_gateway() -> dict[str, object]:
    """A ``kind: gateway`` pi default whose anthropic family serves Claude."""
    return {
        "providers": {
            "rpw-fable": {
                "kind": "gateway",
                "default": ["pi"],
                "anthropic": {
                    "base_url": "https://gw.invalid/anthropic",
                    "api_key": "test-gateway-key",
                    "models": {"default": "databricks-claude-fable-5-1"},
                },
            }
        }
    }


def test_provider_qualified_override_split_to_configured_provider() -> None:
    """An override qualified by the configured provider's name is split.

    The web model picker emits ``<provider>/<model>`` values qualified by the
    omnigent provider name; registering that verbatim renders a slash id no
    endpoint serves. Splitting is silent: the named provider is exactly the
    one serving the session.
    """
    provider = creds.resolve_pi_native_provider(
        model="rpw-fable/databricks-claude-fable-5-1",
        config_loader=_rpw_fable_anthropic_gateway,
    )
    assert provider is not None
    # Prefix split; the gateway-kind provider keeps the id verbatim.
    assert provider.model == "databricks-claude-fable-5-1"
    assert provider.credential_warning is None
    cfg = provider.to_models_config()
    ids = [m["id"] for prov in cfg["providers"].values() for m in prov["models"]]
    assert not [mid for mid in ids if "/" in mid]


def test_override_qualified_by_other_configured_provider_warns() -> None:
    """Naming a configured provider other than the serving one warns.

    The session is served by the pi-default provider, so a model picked from
    another configured provider is requested from the default instead — say
    so rather than silently reinterpreting the value.
    """
    config = _rpw_fable_anthropic_gateway()
    config["providers"]["other-gw"] = {
        "kind": "gateway",
        "anthropic": {
            "base_url": "https://other.invalid/anthropic",
            "api_key": "test-key",
            "models": {"default": "databricks-claude-fable-5-1"},
        },
    }
    provider = creds.resolve_pi_native_provider(
        model="other-gw/databricks-claude-fable-5-1", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.model == "databricks-claude-fable-5-1"
    assert provider.credential_warning is not None
    assert "other-gw" in provider.credential_warning
    assert "rpw-fable" in provider.credential_warning


@pytest.mark.parametrize(
    "model_id",
    ["zai-org/GLM-4.7", "openai/gpt-4o", "moonshotai/kimi-k2.6"],
)
def test_vendor_namespaced_model_id_is_not_split(model_id: str) -> None:
    """A slash id whose prefix is no configured provider survives untouched.

    OpenRouter/LiteLLM-style endpoints route by vendor-namespaced ids
    (``openai/gpt-4o``, ``moonshotai/kimi-k2.6``, ``zai-org/GLM-4.7``); the
    prefix is the endpoint's model namespace, not a provider reference — no
    strip, no warning.
    """
    config = {
        "providers": {
            "deepinfra": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.deepinfra.com/v1/openai",
                    "api_key": "sk-test",
                    "wire_api": "chat",
                    "models": {"default": model_id},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(model=model_id, config_loader=lambda: config)
    assert provider is not None
    assert provider.model == model_id
    assert provider.credential_warning is None


def test_vendor_namespaced_claude_id_is_not_split() -> None:
    """``anthropic/claude-…`` on an anthropic family passes through verbatim."""
    config = {
        "providers": {
            "openrouter": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "base_url": "https://openrouter.invalid/anthropic",
                    "api_key": "sk-test",
                    "models": {"default": "anthropic/claude-opus-4-8"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="anthropic/claude-opus-4-8", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.model == "anthropic/claude-opus-4-8"
    assert provider.credential_warning is None


# ── Gateway/local kinds pass overrides through verbatim; only vendor-direct
#    (key-kind) endpoints strip the mechanical ``databricks-`` prefix. ───────


def test_gateway_override_keeps_databricks_prefix_for_anthropic_family() -> None:
    """A ``databricks-`` override on a gateway-kind provider is sent verbatim.

    A gateway fronting the Databricks AI Gateway is addressed by the prefixed
    endpoint name; stripping yields an id the endpoint answers 404 for. The
    family-default path already passed it through — the override path must
    agree.
    """
    config = {
        "providers": {
            "corp-gateway": {
                "kind": "gateway",
                "default": ["pi"],
                "anthropic": {
                    "base_url": "https://gw.invalid/anthropic",
                    "api_key": "test-gateway-key",
                    "models": {"default": "databricks-claude-fable-5-1"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="databricks-claude-fable-5-1", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.model == "databricks-claude-fable-5-1"
    assert provider.credential_warning is None
    cfg = provider.to_models_config()
    assert "databricks-claude-fable-5-1" in [
        m["id"] for m in cfg["providers"]["omnigent"]["models"]
    ]


def test_local_kind_override_passes_through_verbatim() -> None:
    """A local-kind provider serves its own inventory; overrides pass through."""
    config = {
        "providers": {
            "vllm": {
                "kind": "local",
                "default": True,
                "openai": {
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key": "local",
                    "wire_api": "chat",
                    "models": {"default": "databricks-gpt-5-4"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="databricks-gpt-5-4", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.model == "databricks-gpt-5-4"


def _dual_family_config(anthropic: bool = True, openai: bool = True) -> dict[str, object]:
    """A proxy entry exposing an Anthropic surface, an OpenAI surface, or both."""
    proxy: dict[str, object] = {"kind": "gateway", "default": True}
    if anthropic:
        proxy["anthropic"] = {
            "base_url": "https://litellm.internal.example.com/anthropic",
            "api_key": "sk-a",
        }
    if openai:
        proxy["openai"] = {
            "base_url": "https://litellm.internal.example.com/v1",
            "api_key": "sk-o",
        }
    return {"providers": {"proxy": proxy}}


@pytest.mark.parametrize(
    ("model", "expected_api", "expected_base_url"),
    [
        (
            "claude-sonnet-4-6",
            "anthropic-messages",
            "https://litellm.internal.example.com/anthropic",
        ),
        ("gpt-5-5", "openai-responses", "https://litellm.internal.example.com/v1"),
        ("glm-5-2", "openai-responses", "https://litellm.internal.example.com/v1"),
        # Tokens as "other", but no gateway serves it over Anthropic Messages.
        ("gemini-3-5-flash", "openai-responses", "https://litellm.internal.example.com/v1"),
        ("llama-4-maverick", "openai-responses", "https://litellm.internal.example.com/v1"),
    ],
)
def test_dual_family_provider_matches_model_family(
    model: str, expected_api: str, expected_base_url: str
) -> None:
    """A provider offering both surfaces serves each model from its own family.

    The family loop used to return on the first configured family, so a proxy
    with both surfaces sent every model to Anthropic Messages. A non-translating
    proxy rejects that and the turn hangs with no reply.
    """
    config = _dual_family_config()

    provider = creds.resolve_pi_native_provider(model=model, config_loader=lambda: config)

    assert provider is not None
    assert provider.api == expected_api
    assert provider.base_url == expected_base_url


@pytest.mark.parametrize(
    ("model", "anthropic", "openai", "expected_api"),
    [
        ("gpt-5-5", True, False, "anthropic-messages"),
        ("claude-sonnet-4-6", False, True, "openai-responses"),
    ],
)
def test_single_family_provider_serves_any_model(
    model: str, anthropic: bool, openai: bool, expected_api: str
) -> None:
    """One configured family serves every model — it may be protocol-translating.

    Preferring the model's family must not become a requirement: a LiteLLM
    ``/anthropic`` passthrough serves GPT ids, and an OpenAI-compatible proxy
    serves Claude ids. Excluding them would strand both.
    """
    config = _dual_family_config(anthropic=anthropic, openai=openai)

    provider = creds.resolve_pi_native_provider(model=model, config_loader=lambda: config)

    assert provider is not None
    assert provider.api == expected_api


# ---------------------------------------------------------------------------
# Tests for gateway provider metadata propagation (context/maxTokens/reasoning)
# ---------------------------------------------------------------------------


def test_gateway_provider_config_context_window_flows_to_models_json() -> None:
    """context_window in FamilyConfig propagates to models.json contextWindow.

    When a user configures ``context_window: 1048576`` on their gateway provider
    family, the pi-native provider entry carries that value so Pi uses the real
    limit instead of defaulting to 128k.
    """
    config = {
        "providers": {
            "litellm": {
                "kind": "gateway",
                "openai": {
                    "api_key": "test-key",
                    "base_url": "https://api.example.com",
                    "models": {"default": "glm-5.2"},
                    "wire_api": "chat",
                    "context_window": 1_048_576,
                    "max_output_tokens": 131_072,
                },
                "default": True,
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]
    assert entry["id"] == "glm-5.2"
    assert entry["contextWindow"] == 1_048_576
    assert entry["maxTokens"] == 131_072


def test_gateway_provider_without_limits_still_carries_input_and_reasoning() -> None:
    """A gateway config with no limits still produces a richer entry than bare id.

    Even when ``context_window``/``max_output_tokens`` are absent, the entry
    includes ``input`` and (for reasoning models) ``reasoning: true``.
    """
    config = {
        "providers": {
            "local": {
                "kind": "gateway",
                "openai": {
                    "api_key": "test-key",
                    "base_url": "http://localhost:8080/v1",
                    "models": {"default": "deepseek-r1"},
                    "wire_api": "chat",
                },
                "default": True,
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]
    assert entry["id"] == "deepseek-r1"
    # Even without limits, reasoning and input are populated.
    assert entry.get("reasoning") is True
    assert "input" in entry


def test_gateway_provider_max_output_tokens_validation_rejects_negative() -> None:
    """Negative max_output_tokens is rejected by the provider config parser."""
    from omnigent.errors import OmnigentError
    from omnigent.onboarding.provider_config import load_providers

    config = {
        "providers": {
            "bad-provider": {
                "kind": "gateway",
                "openai": {
                    "api_key": "test-key",
                    "base_url": "https://api.example.com",
                    "models": {"default": "model"},
                    "max_output_tokens": -1,
                },
                "default": True,
            }
        }
    }
    with pytest.raises(OmnigentError, match="max_output_tokens"):
        load_providers(config)


# ---------------------------------------------------------------------------
# _cli_config_pi_provider must prefer live-served Claude models over the catalog
# ---------------------------------------------------------------------------


def _seed_pi_own_login(agent_dir: Path) -> None:
    """Seed a Pi agent dir logged into anthropic, with an extra stale catalog."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "sk-own"}})
    )
    (agent_dir / "models-store.json").write_text(
        json.dumps(
            {
                "anthropic": {
                    "models": [
                        {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5"},
                        {"id": "claude-haiku-4-5"},
                    ],
                    "checkedAt": 1750000000,
                },
                # A provider with cached models but NO auth.json entry: Pi
                # can't drive it, so the picker must not offer it.
                "openai": {"models": [{"id": "gpt-5.2", "name": "GPT 5.2"}]},
            }
        )
    )


def test_model_options_fall_back_to_pi_own_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no managed provider, the picker offers Pi's own logged-in models.

    The launched Pi runs on its own login in that state, so the pre-launch
    catalog is Pi's ``models-store.json`` filtered to logged-in providers,
    qualified ``provider/model`` (the form Pi's ``--model`` resolves).
    """
    monkeypatch.setattr(creds, "resolve_pi_native_provider", lambda: None)
    monkeypatch.setenv(creds.PI_CODING_AGENT_DIR_ENV_VAR, str(tmp_path))
    _seed_pi_own_login(tmp_path)

    assert creds.pi_native_model_options() == [
        {
            "id": "anthropic/claude-haiku-4-5",
            "model": "anthropic/claude-haiku-4-5",
            "displayName": "claude-haiku-4-5",
        },
        {
            "id": "anthropic/claude-sonnet-4-5",
            "model": "anthropic/claude-sonnet-4-5",
            "displayName": "Claude Sonnet 4.5",
        },
    ]


def test_pi_own_login_options_empty_without_login(tmp_path: Path) -> None:
    """No (or an empty) ``auth.json`` means nothing Pi can drive: empty catalog."""
    assert creds.pi_own_login_model_options(agent_dir=tmp_path) == []
    (tmp_path / "auth.json").write_text("{}")
    (tmp_path / "models-store.json").write_text(
        json.dumps({"anthropic": {"models": [{"id": "claude-sonnet-4-5"}]}})
    )
    assert creds.pi_own_login_model_options(agent_dir=tmp_path) == []


def test_pi_own_login_options_tolerate_malformed_files(tmp_path: Path) -> None:
    """Malformed auth/models-store files degrade to an empty catalog, never raise."""
    (tmp_path / "auth.json").write_text("{not json")
    (tmp_path / "models-store.json").write_text("[]")
    assert creds.pi_own_login_model_options(agent_dir=tmp_path) == []


def test_inline_family_registers_curated_shortlist_as_extra_models() -> None:
    """The family's ``models:`` tier map registers as Pi ``extra_models``.

    Pi then shows exactly the deployment's curated, verified set in /model
    (verified empirically: Pi lists only the models its models.json
    declares), instead of just the launch model. Duplicate ids collapse to
    their first occurrence; the selected model is not registered twice.
    """
    config = {
        "providers": {
            "bifrost": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "http://bifrost.example.com/v1",
                    "api_key": "sk-test",
                    "wire_api": "chat",
                    "models": {
                        "default": "GLM-5.3-Flash",
                        "opus": "GLM-5.3",
                        "fable": "claude-fable-5",
                        "pro": "deepseek-v4-pro",
                        "sonnet": "GLM-5.3-Flash",  # duplicate id of the default
                    },
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None

    extra_ids = [entry["id"] for entry in provider.extra_models]
    assert extra_ids == ["GLM-5.3-Flash", "GLM-5.3", "claude-fable-5", "deepseek-v4-pro"]
    by_id = {entry["id"]: entry for entry in provider.extra_models}
    # The gateway entry builder flags reasoning for DeepSeek (reasoning
    # channel) and for Claude (thinking-level controls) on every surface;
    # GLM gets neither.
    assert by_id["deepseek-v4-pro"].get("reasoning") is True
    assert by_id["claude-fable-5"].get("reasoning") is True
    assert "reasoning" not in by_id["GLM-5.3"]

    # The rendered models.json registers the shortlist alongside the launch
    # model, each exactly once (the launch model is not re-appended).
    rendered = provider.to_models_config()
    registered = rendered["providers"][provider.provider_id]["models"]
    assert [entry["id"] for entry in registered] == [
        "GLM-5.3-Flash",
        "GLM-5.3",
        "claude-fable-5",
        "deepseek-v4-pro",
    ]


def test_inline_family_without_models_map_registers_only_the_launch_model() -> None:
    """No ``models:`` map → no shortlist, behaviour unchanged."""
    config = {
        "providers": {
            "bifrost": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "http://bifrost.example.com/v1",
                    "api_key": "sk-test",
                    "wire_api": "chat",
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(model="glm-5.3", config_loader=lambda: config)
    assert provider is not None
    assert [entry["id"] for entry in provider.extra_models] == ["glm-5.3"]


def test_curated_tier_ids_strip_bracket_suffixes() -> None:
    """Tier ids get the same bracket-suffix strip as the selected model.

    A ``[1m]``-style operator id renders (and scopes) as the bare gateway
    model id; a suffixed duplicate of the launch model collapses into the
    launch model's existing entry.
    """
    config = {
        "providers": {
            "bifrost": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "http://bifrost.example.com/v1",
                    "api_key": "sk-test",
                    "wire_api": "chat",
                    "models": {
                        "default": "GLM-5.3-Flash",
                        "opus": "GLM-5.3[16m]",
                        "sonnet": "GLM-5.3-Flash[16m]",  # same stripped id as default
                    },
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    assert [entry["id"] for entry in provider.extra_models] == ["GLM-5.3-Flash", "GLM-5.3"]


def test_curated_tier_alias_resolves_to_concrete_id() -> None:
    """A tier value naming another tier resolves to the concrete id.

    Deployments alias tier names to ids (``deepseek-pro: deepseek-v4-pro``)
    and reference the alias from elsewhere (``default: deepseek-pro``). The
    alias must reach neither the launch model nor the picker's shortlist —
    the gateway serves the id, not the tier name.
    """
    config = {
        "providers": {
            "bifrost": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "http://bifrost.example.com/v1",
                    "api_key": "sk-test",
                    "wire_api": "chat",
                    "models": {
                        "default": "deepseek-pro",  # aliases the tier below
                        "deepseek-pro": "deepseek-v4-pro",
                        "glm": "GLM-5.3",
                    },
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # The launch model is the id, not the alias ...
    assert provider.model == "deepseek-v4-pro"
    # ... and the alias never renders as a picker row of its own.
    assert [entry["id"] for entry in provider.extra_models] == [
        "deepseek-v4-pro",
        "GLM-5.3",
    ]


def test_curated_tier_alias_resolves_before_bracket_strip() -> None:
    """An aliased tier resolves first, then gets the bracket-suffix strip.

    The alias key is matched against the raw map, so a bracketed target
    still collapses to its bare gateway id instead of rendering (or
    launching) with the suffix.
    """
    config = {
        "providers": {
            "bifrost": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "http://bifrost.example.com/v1",
                    "api_key": "sk-test",
                    "wire_api": "chat",
                    "models": {
                        "default": "long-context",  # aliases the tier below
                        "long-context": "GLM-5.3[16m]",
                        "glm": "GLM-5.3-Flash",
                    },
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    assert provider.model == "GLM-5.3"
    assert [entry["id"] for entry in provider.extra_models] == ["GLM-5.3", "GLM-5.3-Flash"]


def test_anthropic_protocol_proxy_serves_non_claude_model() -> None:
    """An Anthropic-protocol proxy may serve non-Claude ids — don't reroute it.

    ``anthropic-messages`` alone does NOT imply "Claude only": a gateway or
    LiteLLM-style proxy speaks that protocol for arbitrary models.
    """
    config = {
        "providers": {
            "proxy": {
                "kind": "gateway",
                "default": True,
                "anthropic": {
                    "base_url": "https://litellm.internal.example.com/anthropic",
                    "api_key": "sk-proxy",
                },
            }
        }
    }

    provider = creds.resolve_pi_native_provider(
        model="zai-org/GLM-4.7", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"

    cfg = provider.to_models_config()
    assert [m["id"] for m in cfg["providers"]["omnigent"]["models"]] == ["zai-org/GLM-4.7"]
