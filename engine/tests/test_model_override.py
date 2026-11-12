"""Unit tests for ``omnigent/model_override.py``.

The validator guards a spawn boundary: a model override is persisted on
the session row and later becomes a ``--model`` argv element (native
CLIs) or a ``HARNESS_<H>_MODEL`` env var (SDK harnesses). These tests
pin the accepted charset, the loud rejections, and the harness-support
map that ``sys_session_send`` consults before forwarding an override.
"""

from __future__ import annotations

import pytest

from omnigent.models.model_override import (
    MODEL_OVERRIDE_MAX_LEN,
    canonical_model_spelling,
    harness_supports_model_override,
    model_family_mismatch,
    normalize_model_for_provider,
    validate_model_override,
)


@pytest.mark.parametrize(
    "value",
    [
        "databricks-claude-sonnet-4-6",
        "claude-opus-4-8[1m]",
        "gpt-5.4-mini",
        "openai/gpt-4o",
        "us.anthropic.claude-sonnet-4-6",
        "databricks/databricks-gpt-5-4",
        "vendor:tag",
        "o3",
    ],
)
def test_validate_model_override_accepts_real_id_shapes(value: str) -> None:
    """
    Every real-world model-id shape passes unchanged.

    A failure means the charset regressed and a legitimate dispatch
    (e.g. a bracketed context-window suffix) would be rejected.
    """
    assert validate_model_override(value) == value


def test_validate_model_override_strips_whitespace() -> None:
    """Surrounding whitespace is stripped, mirroring the PATCH path."""
    assert validate_model_override("  claude-opus-4-8  ") == "claude-opus-4-8"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        # Shell metacharacters must never reach a command line.
        "claude; rm -rf /",
        "claude && evil",
        "model`id`",
        'model"quoted"',
        "model with spaces",
        "model\nnewline",
        # Flag-shaped values could be parsed as a CLI option.
        "--dangerously-skip-permissions",
        "-flag",
        # Length cap.
        "a" * (MODEL_OVERRIDE_MAX_LEN + 1),
    ],
)
def test_validate_model_override_rejects_unsafe_values(value: str) -> None:
    """
    Empty, shell-shaped, flag-shaped, and oversized values fail loud.

    A pass-through here would let a model string smuggle argv/shell
    content across the harness spawn boundary.
    """
    with pytest.raises(ValueError):
        validate_model_override(value)


@pytest.mark.parametrize(
    "harness",
    [
        "claude-sdk",
        # The user-facing alias for claude-sdk must resolve too.
        "claude",
        "pi",
    ],
)
def test_harness_supports_model_override_for_plumbed_harnesses(harness: str) -> None:
    """
    Harnesses with --model / spawn-env plumbing report support.

    A False here would make ``sys_session_send`` reject a model for a
    harness that actually honors it.
    """
    assert harness_supports_model_override(harness) is True


@pytest.mark.parametrize(
    "harness",
    [
        # No model-override plumbing on the runner path: the persisted
        # value would be silently ignored.
        "unknown-harness",
        "totally-unknown",
        None,
    ],
)
def test_harness_supports_model_override_false_for_unplumbed(harness: str | None) -> None:
    """
    Unplumbed / unknown harnesses report no support.

    A True here would silently drop the orchestrator's model choice —
    exactly the failure mode the dispatch-time gate exists to prevent.
    """
    assert harness_supports_model_override(harness) is False


class TestModelFamilyMismatch:
    """model_family_mismatch enforces vendor families per harness."""

    @pytest.mark.parametrize(
        ("harness", "model"),
        [
            ("claude-sdk", "databricks-claude-sonnet-4-6"),
            ("claude", "claude-opus-4-8"),
            ("codex-native", "databricks-gpt-5-4"),
            ("codex", "gpt-5.1-codex"),
            # The OpenAI family is matched as a substring, which is the only
            # rule that admits these real ids: a per-segment match rejects both
            # (the token is glued to a prefix in one and to its generation in
            # the other), which silently un-dispatched them.
            ("codex", "chatgpt-4o-latest"),
            ("codex-native", "gpt4o"),
            ("codex", "databricks-chatgpt-4o-latest"),
            # GLM and Kimi serve on the same OpenResponses wire codex speaks,
            # so those ids are codex-runnable. One row per distinct thing the
            # segment matcher has to get right: bare id, dot-separated gateway
            # namespace, the databricks- prefix, and a non-generational tail.
            ("codex", "glm-5-2"),
            ("native-codex", "system.ai.glm-5-2"),
            ("codex-native", "databricks-kimi-k2-6"),
            ("codex", "kimi-for-coding"),
            ("pi", "databricks-claude-opus-4-8"),
            ("pi", "databricks-gpt-5-4-mini"),
            ("pi", "databricks-meta-llama-3.3-70b-instruct"),
        ],
    )
    def test_compatible_pairs_pass(self, harness: str, model: str) -> None:
        """A matching family (or the multi-model pi harness) returns None."""
        assert model_family_mismatch(harness, model) is None

    @pytest.mark.parametrize(
        ("harness", "model", "expected_rule"),
        [
            ("claude-sdk", "databricks-gpt-5-4", "only runs Claude models"),
            ("claude", "gpt-5.4", "only runs Claude models"),
            ("claude-sdk", "databricks-meta-llama-3.3-70b-instruct", "only runs Claude models"),
            # GLM / Kimi are codex-runnable but not Claude-runnable.
            ("claude-sdk", "databricks-glm-5-2", "only runs Claude models"),
            ("claude-sdk", "system.ai.glm-5-2", "only runs Claude models"),
            ("claude-sdk", "databricks-kimi-k2-6", "only runs Claude models"),
            (
                "codex-native",
                "databricks-claude-sonnet-4-6",
                "only runs codex-compatible models",
            ),
            ("native-codex", "claude-opus-4-8", "only runs codex-compatible models"),
            (
                "codex",
                "databricks-meta-llama-3.3-70b-instruct",
                "only runs codex-compatible models",
            ),
            # A segment merely containing the letters is not the GLM family.
            ("codex", "glmqlfit-eval", "only runs codex-compatible models"),
        ],
    )
    def test_wrong_or_unknown_family_is_rejected(
        self, harness: str, model: str, expected_rule: str
    ) -> None:
        """Cross-family and undeterminable ids are rejected with the rule named.

        The alias case (``claude``) proves canonicalization is
        applied before the family lookup; the llama cases prove an
        undeterminable family fails loud on single-vendor harnesses
        rather than passing through to a gateway error after spawn.
        """
        msg = model_family_mismatch(harness, model)
        assert msg is not None
        assert expected_rule in msg
        assert model in msg

    def test_rejection_names_the_multi_model_fallback(self) -> None:
        """Both single-vendor rejections name pi as the multi-model fallback."""
        claude_msg = model_family_mismatch("claude-sdk", "databricks-gpt-5-4")
        codex_msg = model_family_mismatch("codex-native", "databricks-claude-sonnet-4-6")
        for msg in (claude_msg, codex_msg):
            assert msg is not None
            assert "pi" in msg


@pytest.mark.parametrize("provider_kind", ["key", "subscription"])
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Gateway-local ids lose the prefix for vendor-direct children.
        ("databricks-claude-opus-4-8", "claude-opus-4-8"),
        ("databricks-gpt-5-4", "gpt-5-4"),
        # The stripped remainder must itself be a mechanical claude/gpt
        # id — other families have no canonical vendor counterpart.
        ("databricks-meta-llama-3.3-70b-instruct", "databricks-meta-llama-3.3-70b-instruct"),
        # Already canonical: unchanged.
        ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ],
)
def test_normalize_strips_gateway_prefix_for_vendor_direct_children(
    provider_kind: str, model: str, expected: str
) -> None:
    """
    Vendor-direct children (API key / CLI login) drop the gateway prefix.

    A kept prefix means the vendor API rejects the id; stripping a
    non-claude/gpt remainder would fabricate a vendor id that does not
    exist.

    :param provider_kind: The vendor-direct provider kind under test.
    :param model: The requested model id.
    :param expected: The id that must be persisted.
    """
    assert normalize_model_for_provider(model, provider_kind) == expected


@pytest.mark.parametrize("provider_kind", ["gateway", "local", "none", None])
@pytest.mark.parametrize("model", ["claude-opus-4-8", "databricks-gpt-5-4", "openai/gpt-4o"])
def test_normalize_passes_through_for_unmapped_provider_kinds(
    provider_kind: str | None, model: str
) -> None:
    """
    Non-vendor-direct gateways and undeterminable providers never transform.

    OpenRouter/LiteLLM-style endpoints use their own id namespaces
    (``anthropic/claude-...``), so no transform is mechanical there; an
    undeterminable provider must keep the fail-loud path intact.

    :param provider_kind: The provider kind under test.
    :param model: The requested model id.
    """
    assert normalize_model_for_provider(model, provider_kind) == model


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Mechanical gateway counterparts strip to the canonical id.
        ("databricks-claude-haiku-4-5", "claude-haiku-4-5"),
        ("databricks-gpt-5-4-mini", "gpt-5-4-mini"),
        # Already canonical: unchanged.
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        # Non-claude/gpt remainders have no mechanical counterpart —
        # stripping would fabricate a vendor id that does not exist.
        ("databricks-meta-llama-3.3-70b-instruct", "databricks-meta-llama-3.3-70b-instruct"),
        # Bracket/slash/dotted shapes are not mechanical either.
        ("databricks-claude-opus-4-8[1m]", "databricks-claude-opus-4-8[1m]"),
        ("openai/gpt-4o", "openai/gpt-4o"),
        ("us.anthropic.claude-sonnet-4-6", "us.anthropic.claude-sonnet-4-6"),
    ],
)
def test_canonical_model_spelling(model: str, expected: str) -> None:
    """
    The canonicalizer strips exactly the mechanical ``databricks-``
    prefix and nothing else.

    This is the single spelling-equivalence rule shared by dispatch
    normalization and cost-tier ranking; an over-eager strip here would
    conflate distinct ids, a missed strip would re-open the cost_plan
    false-deny on mixed-fleet tier catalogs.

    :param model: The id to canonicalize.
    :param expected: The canonical spelling.
    """
    assert canonical_model_spelling(model) == expected
