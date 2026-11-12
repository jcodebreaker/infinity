"""Model-override validation helpers shared across runner/server paths.

A per-session model override crosses a spawn boundary: the persisted
value reaches the native CLIs as a ``--model`` argv element at terminal
launch and the SDK harnesses as a ``HARNESS_<H>_MODEL`` env var. The
helpers here keep that string data-only — a conservative model-id
charset rejects anything shell- or flag-shaped before it is persisted.
"""

from __future__ import annotations

import re

from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_availability import CODEX_CANONICAL_HARNESSES
from omnigent.harness_plugins import model_env_keys

# Generous-but-safe upper bound; real ids ("databricks-claude-opus-4-8",
# "us.anthropic.claude-sonnet-4-6") stay well under it.
MODEL_OVERRIDE_MAX_LEN = 256

# First char alphanumeric so the value can never read as a CLI flag
# (``--model --evil``); the tail covers real id shapes: dots
# ("gpt-5.4-mini"), slashes ("openai/gpt-4o"), colons ("vendor:tag"),
# and bracket suffixes ("claude-opus-4-8[1m]").
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$")

# SDK harnesses whose model override lands in the spawn env (their
# ``HARNESS_<H>_MODEL`` key is declared by the harness registry).
_SDK_MODEL_OVERRIDE_HARNESSES = frozenset(model_env_keys())


def validate_model_override(value: str) -> str:
    """
    Validate a caller-supplied model override and return it stripped.

    :param value: Raw model id, e.g. ``"databricks-claude-sonnet-4-6"``.
    :returns: The stripped model id.
    :raises ValueError: If the value is empty, too long, or contains
        characters outside the conservative model-id charset.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("model must be a non-empty string")
    if len(stripped) > MODEL_OVERRIDE_MAX_LEN:
        raise ValueError(f"model exceeds {MODEL_OVERRIDE_MAX_LEN} characters")
    if not _MODEL_ID_RE.fullmatch(stripped):
        raise ValueError(
            "model must start with a letter or digit and contain only "
            "letters, digits, and the characters . _ : / [ ] -"
        )
    return stripped


# Single-vendor harnesses only run their own vendor's models; multi-model
# harnesses (pi) accept any validated id. The executor-type spelling
# ("claude_sdk") that spec_harness() yields when a claude spec declares no
# config harness is listed too.
_CLAUDE_FAMILY_HARNESSES: frozenset[str] = frozenset({"claude-sdk", "claude_sdk"})
# CODEX_CANONICAL_HARNESSES is restricted to the codex-compatible families
# (see is_codex_compatible_model): the gateway serves codex over the
# Anthropic-incompatible Responses wire, and codex >= 0.137 dropped the
# chat/completions wire that was the only path to Claude — so a codex x Claude
# dispatch is genuinely broken and must fail loud here.


# Vendor tokens matched anywhere in the id — the long-standing rule for the
# OpenAI family, kept verbatim so every shape it already accepted still
# dispatches: ``chatgpt-4o-latest`` (the token is glued to a prefix) and
# ``gpt4o`` (glued to its generation) are real OpenAI ids that a per-segment
# match rejects outright.
_CODEX_SUBSTRING_TOKENS: tuple[str, ...] = ("gpt", "codex")

# Tokens matched per segment (``-``/``_``/``.``/``/`` separated) with an
# optional trailing generation number, so ``system.ai.glm-5-2`` and
# ``kimi-k2-instruct`` match while an unrelated endpoint name that merely
# contains the letters (``glmqlfit-eval``) does not. Only the families added
# for codex-compatible routing: three letters inside an arbitrary endpoint name
# is far likelier to be a coincidence than "gpt" is.
_CODEX_COMPATIBLE_SEGMENT_TOKENS: tuple[str, ...] = ("glm", "kimi")

_ID_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")


def is_codex_compatible_model(model: str) -> bool:
    """Report whether *model* can run on a codex harness.

    GPT/codex ids are matched as substrings and GLM/Kimi ids per segment —
    see the token tables above for why the two families are read differently.

    :param model: Model id in any vocabulary, e.g. ``"databricks-glm-5-2"``.
    :returns: ``True`` for the GPT/codex, GLM, and Kimi families.
    """
    lower = model.lower()
    if any(token in lower for token in _CODEX_SUBSTRING_TOKENS):
        return True
    segments = _ID_SEGMENT_SPLIT.split(lower)
    return any(
        re.fullmatch(rf"{token}\d*", segment)
        for segment in segments
        for token in _CODEX_COMPATIBLE_SEGMENT_TOKENS
    )


def model_family_mismatch(harness: str, model: str) -> str | None:
    """
    Return a rejection reason when *model*'s family cannot run on *harness*.

    Family is detected by vendor token: Claude ids contain ``"claude"``
    (``databricks-claude-opus-4-8``); codex-compatible ids name gpt,
    codex, glm, or kimi (``databricks-gpt-5-4``, ``system.ai.glm-5-2``).
    Single-vendor harnesses reject the other family and ids whose family
    cannot be determined — failing loud at dispatch beats an opaque
    harness/gateway error after spawn.
    Multi-model harnesses (pi) accept any validated id.

    :param harness: Harness id from the sub-agent spec, alias or
        canonical, e.g. ``"claude-native"``.
    :param model: Model id that already passed
        :func:`validate_model_override`.
    :returns: Human-readable reason, or ``None`` when compatible.
    """
    canon = canonicalize_harness(harness)
    lower = model.lower()
    is_claude = "claude" in lower
    if canon in _CLAUDE_FAMILY_HARNESSES and not is_claude:
        return (
            f"harness {canon!r} only runs Claude models (id containing "
            f"'claude'); got {model!r}. Use the codex worker for GPT / GLM / "
            "Kimi models or the pi worker for any other "
            "gateway model."
        )
    if canon in CODEX_CANONICAL_HARNESSES and not is_codex_compatible_model(model):
        return (
            f"harness {canon!r} only runs codex-compatible models (id naming "
            f"'gpt', 'codex', 'glm', or 'kimi'); got {model!r}. Use the "
            "claude_code worker for Claude models or the pi worker for any "
            "other gateway model."
        )
    return None


# Bare canonical vendor ids ("claude-opus-4-8", "gpt-5-4", "glm-5-2",
# "kimi-k2-instruct"); slash/colon/bracket/vendor-prefixed shapes have no
# mechanical gateway counterpart. The GLM/Kimi families belong here for the
# same reason the others do: they are dispatchable on codex, so a caller may
# name a bare one, and leaving them out persisted the bare id verbatim onto a
# gateway-backed child that can only serve the prefixed spelling — an opaque
# failure at the CLI instead of a mechanical localization.
_MECHANICAL_VENDOR_ID_RE = re.compile(r"^(?:claude|gpt|glm|kimi)-[a-z0-9][a-z0-9.-]*$")

_DATABRICKS_MODEL_PREFIX = "databricks-"

# Provider kinds whose endpoints take bare canonical vendor ids.
_VENDOR_DIRECT_PROVIDER_KINDS = frozenset({"key", "subscription"})


def canonical_model_spelling(model: str) -> str:
    """
    Return the canonical (gateway-prefix-free) spelling of *model*.

    A bare canonical vendor id and its mechanical ``databricks-``
    counterpart name the same model — :func:`normalize_model_for_provider`
    converts between them per provider — so comparisons that must treat
    the two spellings as equivalent (e.g. cost-tier ranking in
    :mod:`omnigent.util.cost_plan`) compare in this form.

    :param model: A model id, e.g. ``"databricks-claude-haiku-4-5"``.
    :returns: The bare canonical id (``"claude-haiku-4-5"``) when the
        prefix is mechanical; otherwise *model* unchanged (slash/colon/
        bracket shapes, and families outside
        :data:`_MECHANICAL_VENDOR_ID_RE`, have no mechanical gateway
        counterpart).
    """
    if model.startswith(_DATABRICKS_MODEL_PREFIX):
        bare = model[len(_DATABRICKS_MODEL_PREFIX) :]
        if _MECHANICAL_VENDOR_ID_RE.fullmatch(bare):
            return bare
    _SYSTEM_AI_PREFIX = "system.ai."
    if model.startswith(_SYSTEM_AI_PREFIX):
        bare = model[len(_SYSTEM_AI_PREFIX) :]
        if _MECHANICAL_VENDOR_ID_RE.fullmatch(bare):
            return bare
    return model


def normalize_model_for_provider(model: str, provider_kind: str | None) -> str:
    """
    Mechanically localize *model* for the child's resolved provider.

    Runs at the ``sys_session_send`` dispatch gate AFTER the family
    guard, which validates the caller's requested id verbatim (family
    tokens survive this transform in both directions, so the verdict is
    order-independent — checking first keeps error text quoting exactly
    what the caller sent). Two transforms, both prefix-mechanical:

    - Databricks-gateway child + a bare canonical id of a localizable
      family (claude / gpt / glm / kimi) → prepend ``databricks-``
      (``claude-opus-4-8`` → ``databricks-claude-opus-4-8``).
    - Vendor-direct child (API key / CLI subscription) + a
      ``databricks-``-prefixed id of one → strip the prefix
      (``databricks-gpt-5-4`` → ``gpt-5-4``).

    Anything non-mechanical (slash/colon/bracket shapes, families outside
    :data:`_MECHANICAL_VENDOR_ID_RE`, gateway/local/unknown provider kinds)
    passes through unchanged — the existing fail-loud harness/gateway error
    remains the safety net for genuinely unroutable ids.

    :param model: A model id that already passed
        :func:`validate_model_override`, e.g. ``"claude-sonnet-4-6"``.
    :param provider_kind: The child's resolved provider kind from
        :func:`omnigent.models.model_catalog.resolve_model_provider`, e.g.
        ``"key"`` or ``"gateway"``; ``None`` when undeterminable.
    :returns: The localized model id, or *model* unchanged.
    """
    if provider_kind in _VENDOR_DIRECT_PROVIDER_KINDS:
        return canonical_model_spelling(model)
    return model


def harness_supports_model_override(harness: str | None) -> bool:
    """
    Return whether *harness* has per-session model-override plumbing.

    The SDK harnesses receive the override via ``HARNESS_<H>_MODEL`` in the
    spawn env. Anything else (e.g. unknown harnesses) silently ignores the
    persisted value, so callers must reject the override up front.

    :param harness: Harness id from a spec, e.g. ``"pi"`` or
        ``"claude"``; ``None`` when the harness could not be resolved.
    :returns: ``True`` when the override reaches the harness process.
    """
    if harness is None:
        return False
    return canonicalize_harness(harness) in _SDK_MODEL_OVERRIDE_HARNESSES
