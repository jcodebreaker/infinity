"""Unit tests for the runner's summarizer provider-routing helpers.

``resolve_model_provider_family`` and ``normalize_anthropic_base_url`` are
module-level pure functions (``omnigent.runner.app``) used by
``_resolve_local_provider_connection`` / ``_resolve_provider_connection`` to
pick which provider's credentials serve a rollover summarizer call. The
load-bearing property: a model id that is neither recognizably Anthropic nor
recognizably OpenAI must resolve to ``None``, never silently to ``"openai"``
— this connection-resolution subsystem only has an Anthropic or an OpenAI
slot to offer, and guessing wrong hands a third provider's call the wrong
credentials.
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import normalize_anthropic_base_url, resolve_model_provider_family

# ── resolve_model_provider_family ─────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-1",
        "claude-haiku-4-5",
        "anthropic/claude-haiku-4-5",
        "ANTHROPIC/CLAUDE-HAIKU-4-5",
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "anthropic.claude-3-opus-20240229-v1:0",
        "databricks-claude-opus-4-8",
        "DATABRICKS-CLAUDE-OPUS-4-8",
    ],
)
def test_anthropic_ids_resolve_to_anthropic(model: str) -> None:
    assert resolve_model_provider_family(model) == "anthropic"


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "openai/gpt-4o-mini",
        "o1-preview",
        "o3-mini",
        "o4-mini",
        "chatgpt-4o-latest",
        "text-embedding-3-small",
    ],
)
def test_openai_ids_resolve_to_openai(model: str) -> None:
    assert resolve_model_provider_family(model) == "openai"


@pytest.mark.parametrize(
    "model",
    [
        "mistral-large",
        "llama-3-70b-instruct",
        "groq/llama-3-70b",
        "xai/grok-3",
        "gemini-2.0-flash",
        "",
    ],
)
def test_unrecognized_ids_resolve_to_none_not_openai(model: str) -> None:
    """The regression this fix closes: an unrecognized id — including one
    from a real but unsupported third provider — must never default to
    "openai"."""
    assert resolve_model_provider_family(model) is None


# ── normalize_anthropic_base_url ──────────────────────────────────────────


@pytest.mark.parametrize(
    "raw_base_url",
    [
        "https://api.anthropic.com",
        "https://api.anthropic.com/",
        "HTTPS://API.ANTHROPIC.COM",
        "https://API.anthropic.COM/",
    ],
)
def test_bare_anthropic_host_gets_v1_appended(raw_base_url: str) -> None:
    normalized = normalize_anthropic_base_url(raw_base_url)
    assert normalized.rstrip("/").lower().endswith("api.anthropic.com/v1")


def test_already_versioned_anthropic_url_is_untouched() -> None:
    assert (
        normalize_anthropic_base_url("https://api.anthropic.com/v1")
        == "https://api.anthropic.com/v1"
    )


def test_non_anthropic_host_is_untouched() -> None:
    custom = "https://my-proxy.example.com/anthropic"
    assert normalize_anthropic_base_url(custom) == custom


def test_non_anthropic_host_with_no_path_is_untouched() -> None:
    custom = "https://my-llm-gateway.internal.example.com"
    assert normalize_anthropic_base_url(custom) == custom
