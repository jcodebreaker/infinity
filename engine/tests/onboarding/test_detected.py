"""Tests for omnigent.onboarding.detected — ambient → provider bridge.

Covers synthesizing config-shape provider entries from ambient detections,
the read-time merge (explicit wins; detected auto-default per family), and
the adopt set ``configure harnesses`` persists. Detections are constructed as
real :class:`~omnigent.onboarding.ambient.DetectedProvider` objects (not
mocks) so a regression in field handling surfaces here.
"""

from __future__ import annotations

import pytest

from omnigent.onboarding.ambient import DetectedProvider
from omnigent.onboarding.detected import (
    effective_config_with_detected,
    providers_to_adopt,
    synthesize_detected_entries,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    OPENAI_FAMILY,
    get_default_provider,
    load_providers,
)


def _anthropic_key() -> DetectedProvider:
    """An ambient ANTHROPIC_API_KEY detection."""
    return DetectedProvider(
        name="anthropic", kind="key", family=ANTHROPIC_FAMILY, source="$ANTHROPIC_API_KEY"
    )


def _codex_login() -> DetectedProvider:
    """An ambient codex CLI login detection."""
    return DetectedProvider(
        name="codex", kind="subscription", family=OPENAI_FAMILY, source="codex CLI login"
    )


def test_synthesize_env_key_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """An anthropic env key becomes a ``key`` entry with an ``env:`` ref.

    Failure means the detected key wouldn't route (wrong kind/family) or
    would leak as an inline secret instead of an env reference. Clears the
    companion gateway env so the assertion pins the vendor default rather
    than an ambient ``ANTHROPIC_BASE_URL`` from the surrounding shell.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    entries = synthesize_detected_entries([_anthropic_key()])
    assert set(entries) == {"anthropic"}
    parsed = load_providers({"providers": entries})["anthropic"]
    assert parsed.kind == "key"
    assert set(parsed.families) == {"anthropic"}
    # The env var is referenced (env:), never the resolved secret value.
    assert entries["anthropic"]["anthropic"]["api_key_ref"] == "env:ANTHROPIC_API_KEY"
    assert entries["anthropic"]["anthropic"]["base_url"] == "https://api.anthropic.com"


def test_synthesize_env_key_preserves_omnigent_prefixed_source() -> None:
    """A prefixed detection keeps the prefixed env ref in provider config."""
    det = DetectedProvider(
        name="anthropic",
        kind="key",
        family=ANTHROPIC_FAMILY,
        source="$OMNIGENT_ANTHROPIC_API_KEY",
    )
    entries = synthesize_detected_entries([det])

    assert entries["anthropic"]["anthropic"]["api_key_ref"] == ("env:OMNIGENT_ANTHROPIC_API_KEY")


def test_synthesize_env_key_openrouter_uses_vendor_endpoint_and_chat_wire() -> None:
    """A detected OpenRouter key gets OpenRouter's base_url + chat wire.

    OpenRouter is OpenAI-compatible but is NOT api.openai.com and only
    speaks Chat Completions. Failure (defaulting to the openai endpoint or
    omitting the chat wire) is the classic "OpenRouter didn't work" bug.
    """
    det = DetectedProvider(
        name="openrouter", kind="key", family=OPENAI_FAMILY, source="$OPENROUTER_API_KEY"
    )
    entries = synthesize_detected_entries([det])
    openai_block = entries["openrouter"]["openai"]
    assert openai_block["base_url"] == "https://openrouter.ai/api/v1"
    # Chat wire is required — Responses would 404 against OpenRouter.
    assert openai_block["wire_api"] == "chat"
    assert openai_block["api_key_ref"] == "env:OPENROUTER_API_KEY"


def test_synthesize_env_key_openai_honors_openai_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detected ``OPENAI_API_KEY`` adopts a companion ``OPENAI_BASE_URL``.

    The OpenAI SDK reads ``OPENAI_BASE_URL`` to target an OpenAI-compatible
    gateway (e.g. the Databricks AI gateway). Ambient detection must honor
    it; otherwise the env key is synthesized against ``api.openai.com`` and
    every request 401s — the credential is a gateway token, not an OpenAI
    key. This is the regression guard for that intermittent multi-turn 401.
    """
    gateway = "https://example.cloud.databricks.com/ai-gateway/openai/v1"
    monkeypatch.setenv("OPENAI_BASE_URL", gateway)
    det = DetectedProvider(
        name="openai", kind="key", family=OPENAI_FAMILY, source="$OPENAI_API_KEY"
    )
    entries = synthesize_detected_entries([det])
    openai_block = entries["openai"]["openai"]
    assert openai_block["base_url"] == gateway
    assert openai_block["api_key_ref"] == "env:OPENAI_API_KEY"


def test_synthesize_env_key_openai_without_base_url_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent ``OPENAI_BASE_URL``, a detected OpenAI key keeps the vendor default."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    det = DetectedProvider(
        name="openai", kind="key", family=OPENAI_FAMILY, source="$OPENAI_API_KEY"
    )
    entries = synthesize_detected_entries([det])
    assert entries["openai"]["openai"]["base_url"] == "https://api.openai.com/v1"


def test_synthesize_env_key_anthropic_honors_base_url_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detected ``ANTHROPIC_API_KEY`` adopts companion base URL + model.

    An Anthropic-compatible gateway (LiteLLM, …) issues a gateway-scoped key
    that 401s against ``api.anthropic.com`` and serves a non-default model.
    Both the endpoint and the model pin must ride the synthesized provider;
    otherwise native Claude routes to the real API (auth fail) or launches
    without ``--model`` (invalid-model).
    """
    gateway = "https://gateway.example/anthropic"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", gateway)
    monkeypatch.setenv("ANTHROPIC_MODEL", "gateway-served-claude")
    entries = synthesize_detected_entries([_anthropic_key()])
    anthropic_block = entries["anthropic"]["anthropic"]
    assert anthropic_block["base_url"] == gateway
    assert anthropic_block["api_key_ref"] == "env:ANTHROPIC_API_KEY"
    # The model pin lands as the family default so the launch gets ``--model``.
    assert anthropic_block["models"] == {"default": "gateway-served-claude"}


def test_synthesize_env_key_anthropic_without_overrides_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent overrides, a detected Anthropic key keeps the vendor default.

    No ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_MODEL`` means the canonical
    ``https://api.anthropic.com`` endpoint and no pinned model (the spec /
    catalog default picks the model).
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    entries = synthesize_detected_entries([_anthropic_key()])
    anthropic_block = entries["anthropic"]["anthropic"]
    assert anthropic_block["base_url"] == "https://api.anthropic.com"
    # No model pinned — the block carries no ``models`` key.
    assert "models" not in anthropic_block


def test_synthesize_subscription_cli() -> None:
    """A codex CLI login becomes a ``subscription`` entry keyed by its CLI."""
    entries = synthesize_detected_entries([_codex_login()])
    assert entries["codex"] == {"kind": "subscription", "cli": "codex"}


def test_synthesize_skips_familyless_detection() -> None:
    """A detection with no harness family is omitted.

    We may detect a key but have no harness surface for it, so synthesizing
    an entry would create an unroutable provider. Failure means a
    familyless key would show up as a (broken) configured provider.
    """
    familyless = DetectedProvider(
        name="mystery", kind="key", family=None, source="$MYSTERY_API_KEY"
    )
    assert synthesize_detected_entries([familyless]) == {}


def test_synthesize_local_ollama() -> None:
    """A reachable Ollama becomes a ``local`` openai-family entry with /v1."""
    det = DetectedProvider(
        name="ollama", kind="local", family=OPENAI_FAMILY, source="http://localhost:11434"
    )
    entries = synthesize_detected_entries([det])
    assert entries["ollama"]["kind"] == "local"
    # The OpenAI-compatible path is appended to the detected host.
    assert entries["ollama"]["openai"]["base_url"] == "http://localhost:11434/v1"


def test_effective_explicit_wins_on_name() -> None:
    """An explicit provider overrides a detected one of the same name.

    The user's config is authoritative: a hand-written ``anthropic`` entry
    must not be replaced by the synthesized ambient one. Failure means the
    detected entry would clobber the user's pinned model/base_url.
    """
    explicit = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "anthropic": {
                    "base_url": "https://proxy.example.com",
                    "api_key": "$MY_KEY",
                    "models": {"default": "claude-opus-4-7"},
                },
            }
        }
    }
    merged = effective_config_with_detected(explicit, [_anthropic_key()])
    entry = load_providers(merged)["anthropic"]
    # The explicit model survives — the detected entry did not win.
    # (Use family_default_model, which does not resolve the secret.)
    assert entry.family_default_model("anthropic") == "claude-opus-4-7"
    # The raw merged entry keeps the explicit proxy base_url, not the
    # synthesized api.anthropic.com.
    merged_providers = merged["providers"]
    assert merged_providers["anthropic"]["anthropic"]["base_url"] == "https://proxy.example.com"


def test_effective_explicit_default_not_overridden_by_detected() -> None:
    """An explicit family default is never displaced by a detected provider.

    With an explicit anthropic default and a detected OpenRouter (openai)
    key, the anthropic default stays the explicit one while openai's default
    becomes the detected OpenRouter. Failure means auto-default trampled the
    user's chosen Claude default.
    """
    explicit = {
        "providers": {
            "my-claude": {
                "kind": "key",
                "default": True,
                "anthropic": {"base_url": "https://api.anthropic.com", "api_key": "$K"},
            }
        }
    }
    detected = [
        DetectedProvider(
            name="openrouter", kind="key", family=OPENAI_FAMILY, source="$OPENROUTER_API_KEY"
        )
    ]
    merged = effective_config_with_detected(explicit, detected)
    # Explicit anthropic default untouched; openai default = detected openrouter.
    assert get_default_provider(merged, ANTHROPIC_FAMILY).name == "my-claude"
    assert get_default_provider(merged, OPENAI_FAMILY).name == "openrouter"


def test_providers_to_adopt_skips_already_configured() -> None:
    """``providers_to_adopt`` returns only detections not already configured.

    Opening ``configure harnesses`` should persist new detections but never
    duplicate an already-configured name. Failure means re-opening the
    manager would rewrite/duplicate an existing entry.
    """
    explicit = {"providers": {"anthropic": {"kind": "subscription", "cli": "claude"}}}
    adopt = providers_to_adopt(explicit, [_anthropic_key(), _codex_login()])
    # anthropic already configured (by name) → skipped; only codex is new.
    assert set(adopt) == {"codex"}


def _claude_login() -> DetectedProvider:
    """An ambient claude CLI login detection (named by its CLI, ``claude``)."""
    return DetectedProvider(
        name="claude", kind="subscription", family=ANTHROPIC_FAMILY, source="claude CLI login"
    )


def test_providers_to_adopt_skips_subscription_for_configured_cli() -> None:
    """A detected login isn't adopted when its CLI already has a subscription.

    The ambient detector names a Claude login ``"claude"``, but the user may
    have added that same login explicitly under a different name
    (``"claude-subscription"``). Adopting the detection by name would write a
    *second* subscription for the one CLI — the ``claude`` +
    ``claude-subscription`` duplicate. Adoption must skip a detected
    subscription whose CLI is already configured (under any name).
    """
    explicit = {"providers": {"claude-subscription": {"kind": "subscription", "cli": "claude"}}}
    adopt = providers_to_adopt(explicit, [_claude_login(), _codex_login()])
    # claude's CLI is already covered by claude-subscription → not re-adopted;
    # codex is genuinely new.
    assert set(adopt) == {"codex"}


def test_effective_config_skips_duplicate_subscription_for_configured_cli() -> None:
    """The read-time merge never surfaces a detected login twice.

    With an explicit ``claude-subscription`` (the default, as the add flow
    always writes it) and a detected ``claude`` login for the same CLI, the
    merged view keeps only the explicit entry — no synthesized ``claude``
    duplicate alongside it.
    """
    explicit = {
        "providers": {
            "claude-subscription": {"kind": "subscription", "default": True, "cli": "claude"}
        }
    }
    merged = effective_config_with_detected(explicit, [_claude_login()])
    providers = merged["providers"]
    assert "claude" not in providers  # the detected duplicate is dropped
    assert "claude-subscription" in providers
    # The single remaining subscription is still the anthropic default.
    assert get_default_provider(merged, ANTHROPIC_FAMILY).name == "claude-subscription"


# ── cli-config (codex config.toml custom provider) bridging ────────────────


def test_malformed_dismissed_detections_treated_as_empty() -> None:
    """A malformed ``dismissed_detections`` value dismisses nothing.

    A hand-edited scalar (or junk entries) must not crash setup or
    accidentally dismiss everything; the next dismissal write self-heals
    the key into a proper list.
    """
    from omnigent.onboarding.detected import dismissed_detection_names

    assert dismissed_detection_names({"dismissed_detections": "oops"}) == frozenset()
    # Non-string members are ignored; string members still count.
    assert dismissed_detection_names({"dismissed_detections": [3, "codex-databricks"]}) == (
        frozenset({"codex-databricks"})
    )
