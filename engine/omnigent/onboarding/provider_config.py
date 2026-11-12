"""Read/write the kind-typed model-provider config in ``~/.omnigent/config.yaml``.

For users who route coding agents through a model endpoint (a vendor API
key, a subscription CLI login, a gateway like OpenRouter or LiteLLM, or a
local Ollama), the ``providers:``
block in ``~/.omnigent/config.yaml`` is the source of truth for the
active model selection. Defaults are **per family**: a provider marked
**``default: true``** is the default for the family/families it serves,
so a Claude (``anthropic``) default and a Codex (``openai``) default
coexist (at most one default per family). This mirrors how multi-model
tools key defaults by purpose — Zed's per-feature model pointers,
Continue's per-role models. (A per-agent spec can
still select a provider explicitly via ``executor.auth: {type: provider,
name: <name>}`` — independent of these defaults.)

This module is the data layer of the model-selection feature (chunk 1a of
``designs/oss-cuj/04-model-selection-implementation.md``). It extends the
family-based provider shape with:

- an explicit **kind** per provider entry — one of ``key`` /
  ``subscription`` / ``gateway`` / ``local`` — so the
  readout and routing can describe *how* a provider authenticates;
- a **secret reference** (``api_key_ref: env:<VAR>`` or
  ``keychain:<name>``) as an alternative to an inline ``api_key: $VAR``;
- an **introspection** helper (:func:`describe_active_credential`) that
  resolves the active provider + harness into a :class:`ResolvedCredential`
  for the ``/model`` readout and the ``models`` command — never exposing
  the secret value itself.

Each inline-credential provider (``key`` / ``gateway`` / ``local``) groups
one or more *families*:

- ``anthropic`` — the Anthropic Messages API surface, consumed by the
  Claude SDK harness and native Claude Code.
- ``openai`` — the OpenAI Responses/Chat surface, consumed by native Codex.

The ``pi`` harness consumes both families.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Literal

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.spec.parser import check_unresolved_env_vars
from omnigent.util.env_credentials import (
    _ENV_REF_RE,
    env_names_with_omnigent_prefix,
    expand_envvars_with_omnigent_prefix,
    getenv_with_omnigent_prefix,
    omnigent_prefixed_env_name,
)

_logger = logging.getLogger(__name__)

# Family keys. ``anthropic`` is the Messages-API surface (Claude SDK,
# native Claude); ``openai`` is the Responses/Chat surface (native Codex).
# ``pi`` consumes both, preferring them in this order.
ANTHROPIC_FAMILY = "anthropic"
OPENAI_FAMILY = "openai"
_VALID_FAMILIES = (ANTHROPIC_FAMILY, OPENAI_FAMILY)

# The pi harness's *default scope*. pi is not a model family — a provider
# entry never carries a ``pi:`` block — but defaults are scoped per harness
# surface, and pi consumes both families, so it gets its own scope name a
# ``default:`` value may reference (``default: ["anthropic", "pi"]``).
# An inline key/gateway/local (with an anthropic/openai family) drives pi
# directly.
# A ``subscription`` (CLI login, unusable outside its own CLI) and ``bedrock``
# (native-``omnigent claude`` only) can never drive pi — EXCEPT for a pi
# subscription (``kind="subscription", cli="pi"``), which explicitly opts into
# Pi's own native auth and may default the pi surface. Resolution: an explicit
# pi default wins; otherwise pi falls back to the anthropic then openai family
# default, skipping the non-pi kinds (see :func:`default_provider_for_harness`).
PI_SURFACE = "pi"

# Accepted ``wire_api`` values. ``responses`` is the OpenAI Responses API;
# ``chat`` is Chat Completions. Only meaningful for the ``openai`` family
# (the ``anthropic`` family always speaks the Messages API).
RESPONSES_WIRE_API = "responses"
CHAT_WIRE_API = "chat"
_VALID_WIRE_API = (RESPONSES_WIRE_API, CHAT_WIRE_API)

# Provider kinds (the top-level ``kind:`` discriminator on a provider entry).
# - ``key``: a vendor API key reached via families (Anthropic / OpenAI).
# - ``subscription``: a logged-in CLI (``claude`` / ``pi``) — no families,
#   no base_url; the CLI carries its own auth.
# - ``gateway``: an OpenAI/Anthropic-compatible proxy (OpenRouter, LiteLLM).
# - ``local``: a self-hosted endpoint (Ollama, vLLM) reached via families.
KEY_KIND = "key"
SUBSCRIPTION_KIND = "subscription"
GATEWAY_KIND = "gateway"
LOCAL_KIND = "local"
# ``connection: model`` on a gateway provider: the owner's saved Anthropic/OpenRouter key.
MODEL_CONNECTION = "model"
BEDROCK_KIND = "bedrock"
_VALID_KINDS = (
    KEY_KIND,
    SUBSCRIPTION_KIND,
    GATEWAY_KIND,
    LOCAL_KIND,
    BEDROCK_KIND,
)

# Provider kinds that resolve their model/credentials from inline families
# (``key`` / ``gateway`` / ``local``) — as opposed to ``subscription`` (a
# CLI login), which carries no families. _parse_provider dispatches
# subscription first, then treats every remaining kind as a family kind.

ProviderKind = Literal["key", "subscription", "gateway", "local", "bedrock"]

# Maps a canonical harness name to the provider family it consumes. The
# ``pi`` harness consumes both families and so is absent here — callers
# needing a single family for ``pi`` fall back to whichever family the
# active provider configures (see :func:`_family_for_harness`).
_HARNESS_FAMILY: dict[str, str] = {
    "claude-sdk": ANTHROPIC_FAMILY,
    # ``codex`` is the provider-resolution spelling of the Codex CLI's own
    # provider tables (``~/.codex/config.toml``).
    "codex": OPENAI_FAMILY,
}

# Executor-type spellings that ``AgentSpec.harness_kind`` returns for SDK
# harnesses (the executor *type*, not a ``config["harness"]`` value) mapped
# to the canonical harness ids keyed in :data:`_HARNESS_FAMILY`.
_EXECUTOR_TYPE_HARNESS_ALIASES: dict[str, str] = {
    "claude_sdk": "claude-sdk",
}


def provider_family_for_harness(harness: str | None) -> str | None:
    """Return the provider family a harness consumes, or ``None``.

    Maps a harness identifier to ``"anthropic"`` / ``"openai"``. Accepts both
    the canonical ids keyed in :data:`_HARNESS_FAMILY` and the executor-type
    spellings :attr:`AgentSpec.harness_kind` returns for SDK harnesses
    (``"claude_sdk"`` → ``"claude-sdk"``).

    Used to decide whether model settings (``model_override`` /
    ``reasoning_effort``) survive a fork that switches the agent, and whether
    a fork into a native target can carry history (same-family only): a model
    id is provider-bound, so it only transfers within the same family.

    :param harness: A harness id, e.g. ``"claude-sdk"`` or
        ``"claude_sdk"``; ``None`` returns ``None``.
    :returns: ``"anthropic"`` / ``"openai"``, else ``None`` when the harness
        is unknown.
    """
    if harness is None:
        return None
    canonical = canonicalize_harness(harness) or harness
    canonical = _EXECUTOR_TYPE_HARNESS_ALIASES.get(canonical, canonical)
    return harness_family(canonical)


@dataclass(frozen=True)
class ModelPricingConfig:
    """Custom per-million-token pricing for self-hosted models.

    Allows users to specify pricing in their provider config when the
    model isn't in the MLflow catalog (e.g., Ollama, vLLM, custom gateways).
    Prices are specified per million tokens and later converted to per-token
    rates for cost computation.

    All price values must be >= 0. When cache pricing is omitted, the cost
    computation will derive it from the input rate using standard fallback
    ratios (cache read ≈ 0.10×, cache write ≈ 1.25×).

    :param input_per_million: Input price per million tokens (USD), e.g.
        ``0.25`` for $0.25 per 1M input tokens.
    :param output_per_million: Output price per million tokens (USD), e.g.
        ``1.0`` for $1.00 per 1M output tokens.
    :param cache_read_per_million: Optional cache-read (cache-hit) price per
        million tokens (USD). Typically ~0.1× the input rate.
    :param cache_write_per_million: Optional cache-write (cache-creation)
        price per million tokens (USD). Typically ~1.25× the input rate.
    :raises ValueError: If any price value is negative.
    """

    input_per_million: float
    output_per_million: float
    cache_read_per_million: float | None = None
    cache_write_per_million: float | None = None

    def __post_init__(self) -> None:
        """Validate that all pricing values are non-negative."""
        if self.input_per_million < 0:
            raise ValueError(f"input_per_million must be >= 0, got {self.input_per_million}")
        if self.output_per_million < 0:
            raise ValueError(f"output_per_million must be >= 0, got {self.output_per_million}")
        if self.cache_read_per_million is not None and self.cache_read_per_million < 0:
            raise ValueError(
                f"cache_read_per_million must be >= 0, got {self.cache_read_per_million}"
            )
        if self.cache_write_per_million is not None and self.cache_write_per_million < 0:
            raise ValueError(
                f"cache_write_per_million must be >= 0, got {self.cache_write_per_million}"
            )


@dataclass(frozen=True)
class FamilyConfig:
    """One provider family (``anthropic`` or ``openai``) for a harness surface.

    Carries the gateway/endpoint base URL plus exactly one secret source:
    an inline ``api_key`` (possibly a ``$VAR`` reference), an
    ``api_key_ref`` (``env:<VAR>`` / ``keychain:<name>``), or a dynamic
    ``auth_command``. The secret is resolved lazily — see
    :meth:`Provider.family` and :func:`resolve_secret` — so a family the
    active harness does not consume never forces its secret to exist.

    :param base_url: Endpoint base URL the harness talks to, e.g.
        ``"https://openrouter.ai/api/v1"`` (a gateway) or
        ``"http://localhost:11434/v1"`` (a local Ollama). Required. As
        stored on :attr:`Provider.families` this may still contain a raw
        ``$VAR`` reference; it is expanded by :meth:`Provider.family`.
    :param api_key: Inline static API key, possibly a ``$VAR`` reference
        (expanded by :meth:`Provider.family`), e.g. ``"$OPENROUTER_API_KEY"``
        or a literal ``"sk-or-..."``. Mutually exclusive with
        :attr:`api_key_ref` and :attr:`auth_command`.
    :param api_key_ref: A reference to a secret stored outside the config
        file: ``"env:<VAR>"`` (read from the environment) or
        ``"keychain:<name>"`` (read from the omnigent secret store — see
        :func:`resolve_secret` and :mod:`omnigent.onboarding.secrets`),
        e.g. ``"keychain:anthropic"``. Mutually exclusive with
        :attr:`api_key` and :attr:`auth_command`.
    :param auth_command: Shell command that prints a bearer token, for
        short-lived / dynamic tokens, e.g. ``"my-cli print-token"``.
        Mutually exclusive with :attr:`api_key` and :attr:`api_key_ref`.
    :param wire_api: Wire protocol for the ``openai`` family —
        ``"responses"`` or ``"chat"``. ``None`` lets the consuming harness
        pick its own default. Ignored for the ``anthropic`` family.
    :param models: Map of role/tier to model id, with an optional
        ``default`` key consulted when the spec declares no model, e.g.
        ``{"default": "gpt-4o", "opus": "claude-opus-4"}``.
    :param pricing: Optional custom per-million-token pricing for
        self-hosted models and gateways (e.g., Ollama, vLLM, custom endpoints).
        When configured, this pricing takes precedence over catalog lookup,
        allowing self-hosted providers serving catalog-known model IDs to use
        their own rates. ``None`` (the default) means no custom pricing — falls
        back to catalog. See :class:`ModelPricingConfig`.
    :param context_window: Optional context window size in tokens for the
        default model (e.g. ``1048576`` for a 1M-context gateway model).
        When set, pi-native sessions advertise this limit in ``models.json``
        so Pi does not fall back to its own 128k default.
    :param max_output_tokens: Optional maximum output token count for the
        default model (e.g. ``65536``). When set, pi-native sessions
        advertise this limit in ``models.json`` instead of Pi's 16k default.
    """

    base_url: str
    api_key: str | None = None
    api_key_ref: str | None = None
    auth_command: str | None = None
    wire_api: str | None = None
    models: dict[str, str] = field(default_factory=dict)
    pricing: ModelPricingConfig | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None

    @property
    def default_model(self) -> str | None:
        """Return the family's default model id, or ``None``.

        :returns: The ``models["default"]`` entry, e.g. ``"gpt-4o"``, or
            ``None`` when the family declares no default.
        """
        return self.models.get("default")

    def resolve_model_tier(self, model_id: str) -> str:
        """Resolve a ``models:`` value that names another tier to its id.

        Deployments alias tier names to ids (``deepseek-pro:
        deepseek-v4-pro``) and reference those aliases from other keys
        (``default: deepseek-pro``). Whatever reaches an endpoint — the
        launch model, the picker's shortlist, the spawn env — must be the
        concrete id, never the alias.

        :param model_id: A ``models`` key or value, e.g. ``"deepseek-pro"``.
        :returns: The concrete id the alias chain ends at, e.g.
            ``"deepseek-v4-pro"``; *model_id* unchanged when it names no
            other tier.
        """
        current = model_id
        for _ in range(8):  # bounded: a cyclic alias map must terminate
            alias = self.models.get(current)
            if not isinstance(alias, str) or not alias or alias == current:
                return current
            current = alias
        return model_id


@dataclass(frozen=True)
class ProviderEntry:
    """A named, kind-typed provider from the ``providers:`` config block.

    The :attr:`kind` discriminates how the provider authenticates:

    - ``key`` / ``gateway`` / ``local`` carry :attr:`families` (and no
      :attr:`cli`).
    - ``subscription`` carries :attr:`cli` (``"claude"`` / ``"pi"``)
      and no families/base_url — the CLI's own login supplies auth.

    :param name: The provider name as keyed under ``providers:``, e.g.
        ``"anthropic"`` or ``"openrouter"``.
    :param kind: The provider kind, one of ``"key"`` / ``"subscription"``
        / ``"gateway"`` / ``"local"``.
    :param families: Parsed families keyed by family name
        (``"anthropic"`` / ``"openai"``), e.g.
        ``{"openai": FamilyConfig(...)}``. Empty for the ``subscription``
        kind. The ``base_url`` / ``api_key`` values stored
        here may still hold raw ``$VAR`` references — they are expanded
        lazily by :meth:`family`, so consumers should go through
        :meth:`family` rather than indexing :attr:`families` directly.
    :param cli: For ``kind="subscription"``: the CLI whose login carries
        auth, ``"claude"`` or ``"pi"``. ``None`` otherwise.
    :param connection: ``"model"`` on a gateway provider selects the session
        owner's saved model key. ``None`` otherwise.
    :param default_families: The set of model families this provider is
        the **default** for. Sourced from the entry's ``default:`` flag:
        ``true`` → every family it serves (:func:`provider_families`); a
        family name (``default: openai``) or list (``default: [anthropic]``)
        → just those. Empty when not a default. Per-family scoping lets a
        shared provider (gateway / OpenRouter) be the default
        for one harness's surface without claiming the other. At most one
        default may serve a given family (enforced in
        :func:`get_default_provider`). See
        :func:`get_default_provider` / :func:`default_provider_for_harness`.
    """

    name: str
    kind: ProviderKind
    families: dict[str, FamilyConfig] = field(default_factory=dict)
    cli: str | None = None
    connection: str | None = None
    default_families: frozenset[str] = frozenset()

    @property
    def default(self) -> bool:
        """Whether this provider is the default for any family it serves.

        Backward-compatible accessor: ``True`` iff :attr:`default_families`
        is non-empty. Use :attr:`default_families` when the *which family*
        matters (per-harness defaults).

        :returns: ``True`` when the provider is a default for ≥1 family.
        """
        return bool(self.default_families)

    def family(self, name: str) -> FamilyConfig | None:
        """Return the parsed family for *name* with ``$VAR`` refs expanded.

        Expansion (and the fail-loud check for unresolved variables) is
        deferred to here rather than done at read time, so a provider that
        declares a family the current harness does not consume never forces
        the unused family's env var to exist. Only the family actually
        retrieved is expanded. ``api_key_ref`` is resolved here too via
        :func:`resolve_secret`.

        :param name: Family key, e.g. ``"anthropic"`` or ``"openai"``.
        :returns: The :class:`FamilyConfig` with ``base_url`` resolved and,
            when present, the inline ``api_key`` / ``api_key_ref`` resolved
            into :attr:`FamilyConfig.api_key`. ``None`` when this provider
            does not configure that family.
        :raises OmnigentError: If the family references an unset
            environment variable, or a ``keychain:`` reference names a
            secret that is not stored (see :func:`resolve_secret`).
        """
        raw = self.families.get(name)
        if raw is None:
            return None
        return _expand_family(self.name, name, raw)

    def family_default_model(self, name: str) -> str | None:
        """Return a family's default model id **without** resolving credentials.

        Unlike :meth:`family`, this does not touch ``api_key`` /
        ``api_key_ref`` / ``base_url``, so it never forces a secret to
        exist. Used to pick a default model before the consuming harness
        (and thus the required family) is known.

        :param name: Family key, e.g. ``"openai"`` or ``"anthropic"``.
        :returns: The family's ``models["default"]``, e.g. ``"gpt-4o"``,
            or ``None`` when the family is absent or declares no default.
        """
        raw = self.families.get(name)
        return raw.default_model if raw is not None else None


@dataclass(frozen=True)
class ResolvedCredential:
    """A human-readable description of the active credential for a harness.

    Built by :func:`describe_active_credential` for the ``/model`` readout
    and the ``models`` command. It never carries the secret value — only a
    descriptor of where the secret comes from (:attr:`source`).

    :param provider_name: The active provider's name, e.g. ``"anthropic"``.
    :param kind: The provider's kind, e.g. ``"key"`` or ``"subscription"``.
    :param family: The family selected for the harness (``"anthropic"`` /
        ``"openai"``), or ``None`` for kinds without families
        (``subscription``).
    :param model: The resolved model id (override > family default), e.g.
        ``"claude-sonnet-4-6"``, or ``None`` when no model is determinable
        (e.g. a subscription whose model the CLI picks).
    :param source: A non-secret descriptor of where the credential comes
        from, suitable for display, e.g. ``"$ANTHROPIC_API_KEY"``,
        ``"env:ANTHROPIC_API_KEY"``, ``"keychain:anthropic"``,
        or ``"claude CLI login"``.
    :param base_url: The endpoint base URL for inline-family kinds, e.g.
        ``"https://openrouter.ai/api/v1"``, or ``None`` for the
        ``subscription`` kind (no inline base URL).
    """

    provider_name: str
    kind: ProviderKind
    family: str | None
    model: str | None
    source: str
    base_url: str | None


def resolve_secret(ref: str) -> str:
    """Resolve a secret *ref* into its plaintext value, failing loud.

    Accepts three shapes:

    - ``"env:<VAR>"`` — read ``<VAR>`` from the environment.
    - a bare inline ``$VAR`` / ``${VAR}`` reference — expanded via
      :func:`os.path.expandvars` with an unresolved-variable check.
    - ``"keychain:<name>"`` — read ``<name>`` from the omnigent secret
      store (OS keychain, else a ``0600`` JSON file). The store is
      populated by ``omnigent setup`` — see
      :mod:`omnigent.onboarding.secrets`.

    :param ref: The secret reference, e.g. ``"env:OPENROUTER_API_KEY"``,
        ``"$ANTHROPIC_API_KEY"``, or ``"keychain:anthropic"``.
    :returns: The resolved secret value, e.g. ``"sk-or-..."``.
    :raises OmnigentError: If an ``env:`` / ``$VAR`` reference names an
        unset environment variable, or a ``keychain:`` reference names a
        secret that is not stored.
    """
    if ref.startswith("keychain:"):
        name = ref[len("keychain:") :]
        # Imported lazily to avoid a circular import: secrets.py is a leaf
        # module that must not import provider_config.
        from omnigent.onboarding import secrets

        value = secrets.load_secret(name)
        if value is None:
            raise OmnigentError(
                f"no stored secret named {name!r}.",
                code=ErrorCode.INVALID_INPUT,
            )
        return value
    if ref.startswith("env:"):
        var = ref[len("env:") :]
        resolved = getenv_with_omnigent_prefix(var)
        if resolved is None:
            prefixed = omnigent_prefixed_env_name(var)
            fallback = f" or ${prefixed}" if prefixed != var else ""
            raise OmnigentError(
                f"Unresolved environment variable '${var}' referenced by "
                f"'env:{var}'. Set ${var}{fallback} in the environment.",
                code=ErrorCode.INVALID_INPUT,
            )
        _actual_var, value = resolved
        # Strip surrounding whitespace: a key exported with a stray trailing
        # newline (e.g. ``export KEY=$(cat file)``) must not be forwarded
        # verbatim to a harness/SDK, where the padding fails auth.
        return value.strip()
    # Bare inline reference, e.g. "$ANTHROPIC_API_KEY" or a literal value.
    expanded = expand_envvars_with_omnigent_prefix(ref)
    check_unresolved_env_vars(ref, expanded)
    return expanded


def _config_path() -> str:
    """Return the path to the global omnigent config file.

    Respects ``$OMNIGENT_CONFIG_HOME`` for test isolation (matching the
    rest of the onboarding layer).

    :returns: Path to ``config.yaml``, e.g.
        ``"/home/u/.omnigent/config.yaml"``.
    """
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    if config_home:
        return os.path.join(config_home, "config.yaml")
    return os.path.join(os.path.expanduser("~"), ".omnigent", "config.yaml")


def _load_config() -> dict[str, object]:
    """Load and parse the global config file.

    :returns: The parsed YAML mapping, or ``{}`` when the file is missing,
        empty, or unreadable.
    """
    import yaml

    path = _config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _expand(key: str, value: str) -> str:
    """Expand ``$VAR`` references in *value* and fail loud if unresolved.

    :param key: Config key path for error messages, e.g.
        ``"providers.openrouter.openai.api_key"``.
    :param value: The raw value possibly containing ``$VAR`` / ``${VAR}``
        references, e.g. ``"$OPENROUTER_API_KEY"``.
    :returns: The expanded value, e.g. ``"sk-or-..."``.
    :raises OmnigentError: If a referenced variable is unset.
    """
    expanded = expand_envvars_with_omnigent_prefix(value)
    check_unresolved_env_vars(key, expanded)
    return expanded


def _expand_family(provider_name: str, family_name: str, family: FamilyConfig) -> FamilyConfig:
    """Return a copy of *family* with ``base_url`` + the secret resolved.

    Called by :meth:`ProviderEntry.family` so resolution (and the
    fail-loud checks) happen only for the family a harness actually
    consumes. The inline ``api_key`` (``$VAR``) and ``api_key_ref``
    (``env:`` / ``keychain:``) are both collapsed into the returned
    family's :attr:`FamilyConfig.api_key`; an ``auth_command`` is left
    untouched (resolved at the harness boundary, not here).

    :param provider_name: Owning provider name, e.g. ``"openrouter"``
        (for error messages).
    :param family_name: Family key being expanded, e.g. ``"openai"``.
    :param family: The raw-valued :class:`FamilyConfig` from
        :attr:`ProviderEntry.families`.
    :returns: A new :class:`FamilyConfig` with ``base_url`` expanded and,
        when a static secret source is present, ``api_key`` resolved (with
        ``api_key_ref`` cleared).
    :raises OmnigentError: If a referenced environment variable is unset,
        or a ``keychain:`` reference names a secret that is not stored.
    """
    prefix = f"providers.{provider_name}.{family_name}"
    base_url = _expand(f"{prefix}.base_url", family.base_url)
    if family.api_key is not None:
        api_key = _expand(f"{prefix}.api_key", family.api_key)
        return replace(family, base_url=base_url, api_key=api_key)
    if family.api_key_ref is not None:
        resolved = resolve_secret(family.api_key_ref)
        return replace(family, base_url=base_url, api_key=resolved, api_key_ref=None)
    # auth_command (or, defensively, nothing) — leave the secret slot empty.
    return replace(family, base_url=base_url)


def _parse_family(provider_name: str, family_name: str, raw: dict[str, object]) -> FamilyConfig:
    """Parse one family entry under a provider into a :class:`FamilyConfig`.

    Performs **structural** validation only (required ``base_url``, exactly
    one credential source, valid ``wire_api``). ``$VAR`` references and
    ``api_key_ref`` are left **unresolved** here — resolved lazily by
    :meth:`ProviderEntry.family` via :func:`_expand_family`.

    :param provider_name: Owning provider name, e.g. ``"openrouter"``
        (for error messages).
    :param family_name: Family key being parsed, e.g. ``"openai"``.
    :param raw: The raw family mapping, e.g.
        ``{"base_url": "...", "api_key_ref": "keychain:openrouter",
        "models": {...}}``.
    :returns: A populated :class:`FamilyConfig` whose ``base_url`` /
        ``api_key`` may still contain raw ``$VAR`` references and whose
        ``api_key_ref`` is unresolved.
    :raises OmnigentError: If ``base_url`` is missing, the credential
        sources are not exactly one, or ``wire_api`` is invalid.
    """
    prefix = f"providers.{provider_name}.{family_name}"
    base_url_raw = raw.get("base_url")
    if not isinstance(base_url_raw, str) or not base_url_raw:
        raise OmnigentError(
            f"{prefix}.base_url is required and must be a string.",
            code=ErrorCode.INVALID_INPUT,
        )

    # Exactly one of {api_key, api_key_ref, auth_command} must be set.
    api_key_raw = raw.get("api_key")
    api_key_ref_raw = raw.get("api_key_ref")
    auth_command_raw = raw.get("auth_command")
    present = [
        ("api_key", api_key_raw),
        ("api_key_ref", api_key_ref_raw),
        ("auth_command", auth_command_raw),
    ]
    set_names = [n for n, v in present if isinstance(v, str) and v]
    if len(set_names) > 1:
        raise OmnigentError(
            f"{prefix} must set exactly one of 'api_key', 'api_key_ref', or "
            f"'auth_command', not multiple ({', '.join(set_names)}).",
            code=ErrorCode.INVALID_INPUT,
        )
    if not set_names:
        raise OmnigentError(
            f"{prefix} requires one of 'api_key', 'api_key_ref', or 'auth_command'.",
            code=ErrorCode.INVALID_INPUT,
        )

    api_key = api_key_raw if isinstance(api_key_raw, str) and api_key_raw else None
    api_key_ref = api_key_ref_raw if isinstance(api_key_ref_raw, str) and api_key_ref_raw else None
    # auth_command is a user-controlled shell command, not a secret value,
    # so it is never env-expanded here (it may reference env vars that exist
    # only in the harness subprocess).
    auth_command = (
        auth_command_raw if isinstance(auth_command_raw, str) and auth_command_raw else None
    )

    wire_api_raw = raw.get("wire_api")
    wire_api: str | None = None
    if wire_api_raw is not None:
        wire_api = str(wire_api_raw)
        if wire_api not in _VALID_WIRE_API:
            raise OmnigentError(
                f"{prefix}.wire_api must be one of {_VALID_WIRE_API}, got {wire_api!r}.",
                code=ErrorCode.INVALID_INPUT,
            )

    models_raw = raw.get("models")
    models = (
        {str(k): str(v) for k, v in models_raw.items()} if isinstance(models_raw, dict) else {}
    )

    # Parse optional custom pricing for self-hosted models
    pricing_raw = raw.get("pricing")
    pricing: ModelPricingConfig | None = None
    if pricing_raw is not None:
        if not isinstance(pricing_raw, dict):
            raise OmnigentError(
                f"{prefix}.pricing must be a mapping, got {type(pricing_raw).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
        input_price = pricing_raw.get("input_per_million")
        output_price = pricing_raw.get("output_per_million")
        if input_price is None or output_price is None:
            raise OmnigentError(
                f"{prefix}.pricing requires both 'input_per_million' and "
                "'output_per_million' fields.",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            pricing = ModelPricingConfig(
                input_per_million=float(input_price),
                output_per_million=float(output_price),
                cache_read_per_million=(
                    float(pricing_raw["cache_read_per_million"])
                    if "cache_read_per_million" in pricing_raw
                    else None
                ),
                cache_write_per_million=(
                    float(pricing_raw["cache_write_per_million"])
                    if "cache_write_per_million" in pricing_raw
                    else None
                ),
            )
        except ValueError as exc:
            raise OmnigentError(
                f"{prefix}.pricing: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    context_window_raw = raw.get("context_window")
    context_window: int | None = None
    if context_window_raw is not None:
        if not isinstance(context_window_raw, int) or context_window_raw <= 0:
            raise OmnigentError(
                f"{prefix}.context_window must be a positive integer, got {context_window_raw!r}.",
                code=ErrorCode.INVALID_INPUT,
            )
        context_window = context_window_raw

    max_output_tokens_raw = raw.get("max_output_tokens")
    max_output_tokens: int | None = None
    if max_output_tokens_raw is not None:
        if not isinstance(max_output_tokens_raw, int) or max_output_tokens_raw <= 0:
            raise OmnigentError(
                f"{prefix}.max_output_tokens must be a positive integer, "
                f"got {max_output_tokens_raw!r}.",
                code=ErrorCode.INVALID_INPUT,
            )
        max_output_tokens = max_output_tokens_raw

    return FamilyConfig(
        base_url=base_url_raw,
        api_key=api_key,
        api_key_ref=api_key_ref,
        auth_command=auth_command,
        wire_api=wire_api,
        models=models,
        pricing=pricing,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )


def _parse_default_families(
    name: str, default_raw: object, served: set[str], *, pi_capable: bool = False
) -> frozenset[str]:
    """Resolve a raw ``default:`` value into the scopes it applies to.

    Accepts every form the config can carry:

    - ``True`` / ``"true"`` → every family the provider serves.
    - ``False`` / ``"false"`` / absent / ``""`` → not a default.
    - a family name (``"openai"``) → just that family.
    - a list of family names (``["anthropic", "openai"]``) → those.

    A pi-capable provider may additionally name the :data:`PI_SURFACE`
    scope explicitly (``default: ["anthropic", "pi"]``). ``default: true``
    deliberately does **not** expand to the pi scope: ``true`` means "all
    families served", and two coexisting ``default: true`` providers (one
    per family) are valid — if ``true`` claimed pi on both, they would
    collide on the pi slot. The pi scope is only ever claimed explicitly.

    :param name: Provider name, for error messages.
    :param default_raw: The raw ``default`` value from the entry.
    :param served: The model families this provider actually serves; a
        default may only name these (plus ``"pi"`` when *pi_capable*).
    :param pi_capable: Whether this provider's kind can drive the pi
        harness (every kind except ``subscription``), allowing an explicit
        ``"pi"`` in the default scope.
    :returns: The (validated) scopes the provider is the default for.
    :raises OmnigentError: If ``default`` is an unsupported type, or names
        a scope the provider does not serve.
    """
    if default_raw is None or default_raw is False:
        return frozenset()
    if default_raw is True:
        return frozenset(served)
    if isinstance(default_raw, str):
        token = default_raw.strip().lower()
        if token in ("", "false"):
            return frozenset()
        if token == "true":
            return frozenset(served)
        requested = {default_raw.strip()}
    elif isinstance(default_raw, (list, tuple)):
        requested = {str(f).strip() for f in default_raw}
    else:
        raise OmnigentError(
            f"provider {name!r}: 'default' must be true/false, a family name, "
            f"or a list of family names, got {default_raw!r}.",
            code=ErrorCode.INVALID_INPUT,
        )
    # pi is a valid default scope only when the provider is a pi-capable KIND
    # (``pi_capable``); a hand-edited pi scope on any other kind is rejected
    # at parse time rather than failing loudly only at pi launch.
    allowed = served | {PI_SURFACE} if pi_capable else served
    invalid = requested - allowed
    if invalid:
        raise OmnigentError(
            f"provider {name!r}: 'default' names {sorted(invalid)}, which it does "
            f"not serve (serves {sorted(allowed)}).",
            code=ErrorCode.INVALID_INPUT,
        )
    return frozenset(requested)


def _default_raw_value(default_families: frozenset[str], served: set[str]) -> object:
    """Render *default_families* back to the most compact config form.

    Inverse of :func:`_parse_default_families`: the whole served set →
    ``True`` (stays correct if the provider later serves more families is
    NOT a concern — we re-render on each write); a single family → its
    name; several → a sorted list; none → ``None`` (caller omits the key).

    :param default_families: The scopes the provider is the default for.
    :param served: The scopes the provider serves (may include ``"pi"``).
    :returns: ``True``, a family-name ``str``, a sorted ``list[str]``, or
        ``None`` when not a default.
    """
    if not default_families:
        return None
    # ``True`` round-trips as "all model families served" and never claims
    # the pi scope (see _parse_default_families), so a default set that
    # includes pi must stay explicit — rendering it as ``True`` would drop
    # the pi scope on the next parse.
    if PI_SURFACE not in default_families and default_families == frozenset(served) - {PI_SURFACE}:
        return True
    if len(default_families) == 1:
        return next(iter(default_families))
    return sorted(default_families)


def _parse_provider(name: str, raw: dict[str, object]) -> ProviderEntry:
    """Parse one entry under ``providers:`` into a :class:`ProviderEntry`.

    Dispatches on the entry's ``kind:``. ``key`` / ``gateway`` / ``local``
    require at least one family; ``subscription`` requires a ``cli``.

    :param name: The provider name keyed under ``providers:``, e.g.
        ``"anthropic"``.
    :param raw: The raw provider mapping, e.g.
        ``{"kind": "subscription", "cli": "claude"}``.
    :returns: A populated :class:`ProviderEntry`.
    :raises OmnigentError: If ``kind`` is missing/invalid or the
        kind-specific required fields are absent.
    """
    kind_raw = raw.get("kind")
    if not isinstance(kind_raw, str) or kind_raw not in _VALID_KINDS:
        raise OmnigentError(
            f"provider {name!r}: 'kind' is required and must be one of "
            f"{_VALID_KINDS}, got {kind_raw!r}.",
            code=ErrorCode.INVALID_INPUT,
        )
    kind: ProviderKind = kind_raw  # type: ignore[assignment]  # validated against _VALID_KINDS above

    # The (possibly family-scoped) default flag — resolved per-kind below
    # once the served families are known. The repo's YAML loader strips
    # implicit scalar resolvers (the Norway-problem guard in
    # omnigent/harnesses/loader.py), so a YAML ``default: true`` arrives as
    # the string ``"true"``; the programmatic config-writing path uses a
    # real bool / family-name / list — :func:`_parse_default_families`
    # accepts all forms. The per-family "at most one default per family"
    # invariant is enforced in :func:`get_default_provider`.
    default_raw = raw.get("default")

    if kind == SUBSCRIPTION_KIND:
        cli_raw = raw.get("cli")
        if not isinstance(cli_raw, str) or not cli_raw:
            raise OmnigentError(
                f"provider {name!r}: a 'cli' (e.g. 'claude' or 'pi') is "
                "required when kind is 'subscription'.",
                code=ErrorCode.INVALID_INPUT,
            )
        # A claude subscription serves the anthropic family; a pi subscription
        # serves no model family directly — it is pi-capable so it can claim
        # the pi scope explicitly, signalling "use Pi's own native auth"; an
        # unknown CLI serves nothing.
        served = {ANTHROPIC_FAMILY} if cli_raw == "claude" else set()
        # A claude subscription is locked to its own CLI and cannot drive pi.
        # A pi subscription explicitly opts into Pi's own native auth and may
        # default the pi surface (pi_capable=True, served={}).
        return ProviderEntry(
            name=name,
            kind=kind,
            cli=cli_raw,
            default_families=_parse_default_families(
                name, default_raw, served, pi_capable=(cli_raw == "pi")
            ),
        )

    if kind == "gateway" and raw.get("connection") is not None:
        # ``connection: model``: the session owner's own provider key, held by the engine and
        # reached through its model proxy. Nothing is resolved here; the inference snapshot
        # (server/inference_catalog.py) supplies the proxy base_url + auth command per owner.
        if raw["connection"] != MODEL_CONNECTION:
            raise OmnigentError(
                f"provider {name!r}: a gateway connection must be 'model'.",
                code=ErrorCode.INVALID_INPUT,
            )
        return ProviderEntry(
            name=name,
            kind=kind,
            connection=MODEL_CONNECTION,
            default_families=_parse_default_families(
                name, default_raw, {ANTHROPIC_FAMILY}, pi_capable=True
            ),
        )

    # Inline-family kinds: key / gateway / local.
    families: dict[str, FamilyConfig] = {}
    for family_name in _VALID_FAMILIES:
        family_raw = raw.get(family_name)
        if isinstance(family_raw, dict):
            families[family_name] = _parse_family(name, family_name, family_raw)
    if not families:
        raise OmnigentError(
            f"provider {name!r} (kind {kind!r}) configures no 'anthropic' or 'openai' family.",
            code=ErrorCode.INVALID_INPUT,
        )
    return ProviderEntry(
        name=name,
        kind=kind,
        families=families,
        default_families=_parse_default_families(
            name, default_raw, set(families), pi_capable=True
        ),
    )


def load_config() -> dict[str, object]:
    """Load the global ``~/.omnigent/config.yaml`` mapping.

    Public entry point for callers (e.g. the runtime spawn-env builders)
    that need to pass the parsed config into :func:`load_providers` /
    :func:`default_provider_for_harness`. Respects
    ``$OMNIGENT_CONFIG_HOME`` for test isolation, exactly like the rest
    of the onboarding layer, so a single set of providers drives both the
    readout and the routing path.

    :returns: The parsed config mapping, e.g.
        ``{"providers": {"openrouter": {"kind": "gateway", ...}}}``, or
        ``{}`` when the config file is missing, empty, or unreadable.
    """
    from omnigent.inference_config import load_runtime_inference_config

    return load_runtime_inference_config()


def load_providers(config: dict[str, object]) -> dict[str, ProviderEntry]:
    """Parse the ``providers:`` block of *config* into named entries.

    :param config: The parsed ``~/.omnigent/config.yaml`` mapping, e.g.
        ``{"providers": {"anthropic": {"kind": "key", ...}}, "auth": {...}}``.
    :returns: Providers keyed by name, e.g.
        ``{"anthropic": ProviderEntry(...)}``. Empty when no ``providers:``
        block is present.
    :raises OmnigentError: If any provider entry is malformed.
    """
    providers_raw = config.get("providers")
    if not isinstance(providers_raw, dict):
        return {}
    result: dict[str, ProviderEntry] = {}
    for name, raw in providers_raw.items():
        if not isinstance(raw, dict):
            raise OmnigentError(
                f"provider {str(name)!r} must be a mapping.",
                code=ErrorCode.INVALID_INPUT,
            )
        result[str(name)] = _parse_provider(str(name), raw)
    return result


def provider_credential_env_vars(
    config: dict[str, object], *, include_dollar_key_refs: bool = False
) -> frozenset[str]:
    """Return the env var names referenced by provider ``api_key_ref`` entries.

    Scans all inline-family providers (``key`` / ``gateway`` / ``local``) in
    *config* and collects the names of environment variables they reference for
    credentials, including the ``OMNIGENT_``-prefixed alias for each.  Two
    reference shapes are recognised:

    - ``api_key_ref: env:<VAR>`` — the explicit env-ref form.
    - ``api_key: $VAR`` / ``api_key: ${VAR}`` — an inline ``$VAR`` reference
      stored in the raw ``api_key`` field before lazy expansion.

    This is used by the runner-spawn layer to automatically forward custom
    credential env vars into the runner subprocess without requiring the user
    to list them in ``OMNIGENT_RUNNER_ENV_PASSTHROUGH`` by hand.

    Saved sandbox profiles also include dollar references in ``api_key_ref``
    when ``include_dollar_key_refs`` is enabled. ``keychain:`` refs resolve
    through the secret store and are never env vars.  ``auth_command`` is a
    shell command, not a static env var.  ``base_url`` env-refs are omitted
    because the URL is not a credential.

    :param config: The parsed ``~/.omnigent/config.yaml`` mapping.
    :param include_dollar_key_refs: Forward dollar-style ``api_key_ref`` entries
        from a saved sandbox profile; defaults to legacy forwarding behavior.
    :returns: Env var names (and their ``OMNIGENT_`` aliases) that provider
        credential fields reference, e.g.
        ``frozenset({"MY_TOKEN", "OMNIGENT_MY_TOKEN"})``.
    """
    names: set[str] = set()
    for entry in load_providers(config).values():
        for family in entry.families.values():
            # api_key_ref: env:VAR — explicit env reference.
            if family.api_key_ref is not None and family.api_key_ref.startswith("env:"):
                var = family.api_key_ref[len("env:") :]
                for n in env_names_with_omnigent_prefix(var):
                    names.add(n)
            # api_key: $VAR or ${VAR} — inline $VAR reference (unresolved at
            # parse time; expanded lazily by _expand_family).
            dollar_refs = [family.api_key]
            if (
                include_dollar_key_refs
                and family.api_key_ref is not None
                and family.api_key_ref.startswith("$")
            ):
                dollar_refs.append(family.api_key_ref)
            for reference in dollar_refs:
                if reference is None:
                    continue
                for match in _ENV_REF_RE.finditer(reference):
                    var = match.group(1) or match.group(2)
                    for n in env_names_with_omnigent_prefix(var):
                        names.add(n)
    return frozenset(names)


def harness_family(harness: str) -> str | None:
    """Return the single model family a harness consumes, or ``None``.

    Public accessor for the canonical harness→family map. ``None`` means
    the harness is unmapped or spans both families (e.g. ``pi``), so callers
    filtering by surface should treat ``None`` as "both / no single family".

    :param harness: The canonical harness name, e.g. ``"codex"`` or ``"pi"``.
    :returns: ``"anthropic"`` / ``"openai"`` for a single-family harness, or
        ``None`` for an unmapped / both-family harness.
    """
    return _HARNESS_FAMILY.get(harness)


def provider_families(entry: ProviderEntry) -> frozenset[str]:
    """Return the model families *entry* can serve.

    Defaults are scoped **per family** (the Claude/``anthropic`` surface
    vs the Codex/``openai`` surface), mirroring how multi-model tools key
    defaults by purpose (Zed's per-feature model pointers, Continue's
    per-role models). This reports which families a
    provider is a default *candidate* for:

    - ``key`` / ``gateway`` / ``local``: the families it declares inline,
      plus the :data:`PI_SURFACE` scope (pi consumes either family).
    - ``subscription``: derived from the CLI — ``claude`` serves the
      ``anthropic`` surface, ``pi`` serves only the :data:`PI_SURFACE` scope
      (signals "use Pi's own native auth"). Other CLIs serve nothing.

    :param entry: The provider entry to classify.
    :returns: The scope names this provider can be the default for, e.g.
        ``frozenset({"anthropic"})`` for a Claude subscription.
    """
    if entry.kind == BEDROCK_KIND:
        # Bedrock mode is native-``omnigent claude`` only — the in-process /
        # gateway harnesses (incl. pi) reject it (see
        # configure_agent_harness_with_provider). Surface only its real
        # family (anthropic), never the pi scope.
        return frozenset(entry.families)
    if entry.kind in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
        return frozenset(entry.families) | {PI_SURFACE}
    if entry.kind == SUBSCRIPTION_KIND:
        if entry.cli == "claude":
            return frozenset({ANTHROPIC_FAMILY})
        if entry.cli == "pi":
            # A pi subscription signals "use Pi's own native auth" — it serves
            # only the pi scope (no model family directly).
            return frozenset({PI_SURFACE})
        if entry.cli == "codex":
            return frozenset({OPENAI_FAMILY})
        return frozenset()
    return frozenset()


def get_default_provider(config: dict[str, object], family: str) -> ProviderEntry | None:
    """Return the ``default: true`` provider serving *family*, if any.

    Defaults are per-family: a Claude (``anthropic``) default and a Codex
    (``openai``) default coexist independently. At most one default may
    serve a given family.

    :param config: The parsed config mapping (``providers:`` block).
    :param family: The scope to resolve a default for, ``"anthropic"``,
        ``"openai"``, or the explicit :data:`PI_SURFACE` scope (``"pi"``).
    :returns: The default :class:`ProviderEntry` serving *family*, or
        ``None`` when none is marked default for it.
    :raises OmnigentError: If any provider is malformed, or more than one
        ``default: true`` provider serves *family*.
    """
    candidates = [
        entry for entry in load_providers(config).values() if family in entry.default_families
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        names = ", ".join(sorted(e.name for e in candidates))
        raise OmnigentError(
            f"multiple providers set 'default: true' for the {family!r} family "
            f"({names}); at most one may be the default per family.",
            code=ErrorCode.INVALID_INPUT,
        )
    return candidates[0]


def first_available_provider(config: dict[str, object], family: str) -> ProviderEntry | None:
    """
    Return the first configured provider that can serve *family*, or ``None``.

    Unlike :func:`get_default_provider` (which requires an explicit
    ``default:``), this returns the first provider whose served families
    include *family* regardless of default status — the credential a launch
    falls back to when no default is configured for the family. Shared by the
    runtime spawn-env builders (so a head still launches) and the REPL startup
    creds line (so the readout names exactly what the launch will use), keeping
    the two provably in agreement.

    :param config: The parsed config mapping, optionally merged with ambient
        detections (:func:`effective_config_with_detected`).
    :param family: The model family / surface, e.g. ``"openai"`` or
        :data:`PI_SURFACE`.
    :returns: The first :class:`ProviderEntry` serving *family*, in config
        order, or ``None`` when none serves it.
    """
    for entry in load_providers(config).values():
        if family in provider_families(entry):
            return entry
    return None


def default_provider_for_harness(config: dict[str, object], harness: str) -> ProviderEntry | None:
    """Return the default provider for *harness* (resolving its family).

    Maps the harness to its family (claude-sdk/native-claude→anthropic;
    codex/native-codex→openai) and returns that family's
    default. The ``pi`` harness (and any unmapped harness) consumes both
    families: an explicit :data:`PI_SURFACE` default wins; otherwise it
    falls back to the ``anthropic`` then ``openai`` family default,
    skipping ``subscription`` and ``bedrock`` defaults (a CLI login is
    unusable outside its own CLI, and ``bedrock`` is native-``omnigent
    claude`` only — routing pi to either fails).

    :param config: The parsed config mapping (``providers:`` block).
    :param harness: The canonical harness name, e.g. ``"claude-sdk"`` or
        ``"pi"``.
    :returns: The default :class:`ProviderEntry` for that harness's family,
        or ``None`` when none is configured.
    :raises OmnigentError: If a provider is malformed, or more than one
        default serves the resolved family.
    """
    family = _HARNESS_FAMILY.get(harness)
    if family is not None:
        return get_default_provider(config, family)
    # Unmapped (e.g. pi): an explicit pi-scope default is authoritative.
    explicit = get_default_provider(config, PI_SURFACE)
    if explicit is not None:
        return explicit
    # Fall back across the pi-capable surfaces — prefer anthropic's default,
    # and skip the CLI-bound kinds (unusable outside their own CLI).
    for fam in _VALID_FAMILIES:
        provider = get_default_provider(config, fam)
        if provider is None:
            continue
        # Subscription logins live in the claude/codex CLI's own login, which
        # an unmapped harness doesn't wrap; a bedrock provider is
        # native-``omnigent claude`` only (configure_agent_harness_with_provider
        # raises for it). Neither can serve pi, so skip them and fall through —
        # otherwise a bedrock Claude default would turn a working pi run (own
        # login) into a hard error.
        if provider.kind in (SUBSCRIPTION_KIND, BEDROCK_KIND):
            continue
        return provider
    return None


def surface_default_provider(config: dict[str, object], surface: str) -> ProviderEntry | None:
    """Return the *effective* default provider for a harness surface.

    The display-side companion to :func:`default_provider_for_harness`,
    keyed by surface name rather than harness id: the ``anthropic`` /
    ``openai`` surfaces resolve their explicit per-family default, and the
    :data:`PI_SURFACE` surface resolves the pi harness's effective default
    (explicit pi scope, else the cross-family fallback). Used by the
    ``setup`` harness menus and the REPL startup header so every surface
    shows the provider its harness would actually route through.

    :param config: The parsed config mapping (``providers:`` block).
    :param surface: ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: The effective default :class:`ProviderEntry` for *surface*,
        or ``None`` when none resolves.
    :raises OmnigentError: If a provider is malformed, or more than one
        default serves a scope.
    """
    if surface == PI_SURFACE:
        return default_provider_for_harness(config, PI_SURFACE)
    return get_default_provider(config, surface)


def surface_default_model(entry: ProviderEntry, surface: str) -> str | None:
    """Return the default model *entry* yields for a harness surface.

    For a model family this is that family's ``models.default``. For the
    :data:`PI_SURFACE` surface — pi consumes whichever family supplies its
    credential, anthropic preferred (see :func:`_family_for_harness`) —
    it is the first configured family's default in that same order.

    :param entry: The provider entry whose model is displayed.
    :param surface: ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: The default model id, e.g. ``"claude-sonnet-4-6"``, or
        ``None`` when the relevant family declares no default (or, for
        ``subscription`` / ``databricks`` kinds, always — the CLI /
        profile picks the model).
    """
    if surface != PI_SURFACE:
        return entry.family_default_model(surface)
    for family_name in _VALID_FAMILIES:
        if family_name in entry.families:
            return entry.family_default_model(family_name)
    return None


def _family_for_harness(provider: ProviderEntry, harness: str) -> str | None:
    """Pick the family name *provider* exposes for *harness*.

    Uses the canonical harness→family map; for the ``pi`` harness (which
    consumes both families) or an unmapped harness, falls back to whichever
    single family the provider configures (anthropic preferred).

    :param provider: The active provider entry.
    :param harness: The canonical harness name, e.g. ``"claude-sdk"`` or
        ``"pi"``.
    :returns: ``"anthropic"`` / ``"openai"`` when the provider configures a
        matching family, else ``None``.
    """
    preferred = _HARNESS_FAMILY.get(harness)
    if preferred is not None and preferred in provider.families:
        return preferred
    if preferred is not None:
        # Harness wants a specific family the provider does not configure.
        return None
    # Unmapped harness (e.g. "pi"): use anthropic if present, else openai.
    for family_name in _VALID_FAMILIES:
        if family_name in provider.families:
            return family_name
    return None


def _source_descriptor(family: FamilyConfig) -> str:
    """Return a non-secret descriptor of a family's credential source.

    :param family: The (raw, unexpanded) family config.
    :returns: A display string, e.g. ``"$ANTHROPIC_API_KEY"``,
        ``"env:OPENROUTER_API_KEY"``, ``"keychain:anthropic"``, or an
        auth-command descriptor like ``"auth_command: my-cli token"``.
    :raises OmnigentError: If the family has no credential source (should
        not happen post-parse).
    """
    if family.api_key is not None:
        # Show the $VAR reference verbatim, not the resolved secret.
        return family.api_key
    if family.api_key_ref is not None:
        return family.api_key_ref
    if family.auth_command is not None:
        return f"auth_command: {family.auth_command}"
    raise OmnigentError(
        "provider family has no credential source.",
        code=ErrorCode.INVALID_INPUT,
    )


def describe_active_credential(
    config: dict[str, object],
    harness: str,
    model_override: str | None = None,
) -> ResolvedCredential | None:
    """Describe the active credential for *harness*, for the readout.

    Resolves the default provider for *harness*'s family (per-family
    defaults — see :func:`default_provider_for_harness`), resolves the
    model (``model_override`` beats the family default), and fills a
    :class:`ResolvedCredential` whose :attr:`ResolvedCredential.source` is
    a non-secret descriptor. Never expands or returns the secret value
    itself, so it is safe to call for a family whose secret is unset.

    :param config: The parsed config mapping (``providers:`` block).
    :param harness: The canonical harness name the credential is for, e.g.
        ``"claude-sdk"`` or ``"codex"``.
    :param model_override: An in-session ``/model`` override that beats the
        family default, e.g. ``"openai/gpt-4o"``. ``None`` to use the
        provider's configured default.
    :returns: A :class:`ResolvedCredential`, or ``None`` when no default
        provider serves *harness*'s family.
    :raises OmnigentError: If the resolved provider is malformed, or more
        than one default serves the family.
    """
    provider = default_provider_for_harness(config, harness)
    if provider is None:
        return None

    if provider.kind == SUBSCRIPTION_KIND:
        # The CLI login carries auth; the CLI picks the model unless
        # overridden in-session. No family / base_url.
        return ResolvedCredential(
            provider_name=provider.name,
            kind=provider.kind,
            family=None,
            model=model_override,
            source=f"{provider.cli} CLI login",
            base_url=None,
        )

    # Inline-family kinds: key / gateway / local.
    family_name = _family_for_harness(provider, harness)
    if family_name is None:
        return None
    raw_family = provider.families[family_name]
    model = model_override or raw_family.default_model
    return ResolvedCredential(
        provider_name=provider.name,
        kind=provider.kind,
        family=family_name,
        model=model,
        source=_source_descriptor(raw_family),
        # base_url may contain a raw $VAR; expand it for display only.
        base_url=_expand(f"providers.{provider.name}.{family_name}.base_url", raw_family.base_url),
    )


def set_default_provider(
    providers: dict[str, object], name: str, family: str | None = None
) -> dict[str, object]:
    """Return a copy of ``providers:`` with *name* the default for *family*.

    Marks *name* the default for the chosen family/families and removes
    only that family/those families from every other provider's default —
    so a Claude (``anthropic``) default and a Codex (``openai``) default
    coexist untouched (Zed's "don't disturb the other slot"). When a shared
    provider (gateway / Databricks) loses one family it keeps the other:
    its ``default: true`` is rewritten to the remaining family. Defaults are
    marked **inline**, so this is a read-modify-write of the whole
    ``providers:`` block — the caller writes the return value back wholesale.

    :param providers: The current raw ``providers:`` mapping (config shape),
        e.g. ``{"anthropic": {"kind": "key", "default": true, ...},
        "openrouter": {"kind": "gateway", ...}}``.
    :param name: The provider to make the default, e.g. ``"openrouter"``.
    :param family: The single scope to make the default for — ``"anthropic"``,
        ``"openai"``, or ``"pi"`` (the per-harness path). ``None`` makes
        *name* the default for **all** scopes it serves (the legacy
        whole-provider behavior), clearing those scopes from siblings.
    :returns: A new ``providers:`` mapping with *name* default for the chosen
        family/families and that scope cleared on the others.
    :raises OmnigentError: If *name* is absent, an entry is malformed, or
        *family* is given but *name* does not serve it.
    """
    if name not in providers:
        raise OmnigentError(
            f"cannot set default: provider {name!r} is not declared under 'providers:'.",
            code=ErrorCode.INVALID_INPUT,
        )
    target_raw = providers[name]
    if not isinstance(target_raw, dict):
        raise OmnigentError(f"provider {name!r} must be a mapping.", code=ErrorCode.INVALID_INPUT)
    target_served = provider_families(_parse_provider(name, target_raw))
    # The families to (re)assign to *name*: a single scoped family, or all
    # it serves when unscoped.
    if family is None:
        scope = set(target_served)
    else:
        if family not in target_served:
            raise OmnigentError(
                f"cannot set default: provider {name!r} does not serve the "
                f"{family!r} family (serves {sorted(target_served)}).",
                code=ErrorCode.INVALID_INPUT,
            )
        scope = {family}

    result: dict[str, object] = {}
    for provider_name, entry in providers.items():
        if not isinstance(entry, dict):
            result[provider_name] = entry
            continue
        parsed = _parse_provider(provider_name, entry)
        served = provider_families(parsed)
        if provider_name == name:
            new_defaults = parsed.default_families | scope
        else:
            # Other providers lose exactly the scoped family/families.
            new_defaults = parsed.default_families - scope
        body = {k: v for k, v in entry.items() if k != "default"}
        raw_value = _default_raw_value(new_defaults, set(served))
        if raw_value is not None:
            body["default"] = raw_value
        result[provider_name] = body
    return result


def provider_entry_settings(
    name: str, entry: dict[str, object], *, make_default: bool
) -> dict[str, object]:
    """Build a ``{"providers": {name: entry}}`` dict to merge into config.

    Packages a single provider entry ready to deep-merge into
    ``~/.omnigent/config.yaml`` under ``providers:``. When *make_default*
    is set, the entry carries ``default: true`` — but the caller must still
    clear the flag on any other provider (use :func:`set_default_provider`
    over the merged result), since a deep-merge does not touch siblings.

    :param name: Provider name to key under ``providers:``, e.g.
        ``"openrouter"``.
    :param entry: The provider entry body (already in config shape, without
        the ``default`` key), e.g. ``{"kind": "gateway", "openai":
        {"base_url": "...", "api_key_ref": "keychain:openrouter"}}``.
    :param make_default: Whether to mark this entry ``default: true``.
    :returns: A settings dict with a single ``providers`` key, e.g.
        ``{"providers": {"openrouter": {"kind": "gateway", "default": true,
        ...}}}``.
    """
    body = {k: v for k, v in entry.items() if k != "default"}
    if make_default:
        body["default"] = True
    return {"providers": {name: body}}


# ── Provider entry builders (config shape) ───────────────────────────


# Default endpoint base URL per family for a ``key`` provider (a direct
# vendor key, not a gateway). A gateway/local provider supplies its own
# base_url; a ``key`` provider talks to the canonical vendor endpoint.
_FAMILY_DEFAULT_BASE_URL: dict[str, str] = {
    ANTHROPIC_FAMILY: "https://api.anthropic.com",
    OPENAI_FAMILY: "https://api.openai.com/v1",
}


@dataclass(frozen=True)
class _VendorEndpoint:
    """A direct-``key`` vendor's OpenAI-compatible endpoint + wire protocol.

    :param base_url: The vendor's own API base URL, e.g.
        ``"https://openrouter.ai/api/v1"`` — NOT ``api.openai.com``.
    :param wire_api: The OpenAI wire protocol the vendor speaks —
        ``"chat"`` for the OpenAI-compatible Chat Completions surface every
        third-party vendor below exposes (none implement the OpenAI
        Responses API).
    """

    base_url: str
    wire_api: str


# Per-vendor endpoint + wire for direct ``key`` providers that are NOT the
# canonical OpenAI / Anthropic endpoints. These OpenAI-compatible third
# parties are reached at their OWN base_url (the catalog has no base_url, and
# the openai-family default ``api.openai.com`` is wrong for them — the reason
# an OpenRouter key "didn't work"). All speak Chat Completions, not the
# Responses API. ``openai`` / ``anthropic`` are intentionally absent: they use
# :func:`default_base_url_for_family` (and openai keeps the Responses default).
_KEY_PROVIDER_ENDPOINT: dict[str, _VendorEndpoint] = {
    "openrouter": _VendorEndpoint("https://openrouter.ai/api/v1", CHAT_WIRE_API),
    "groq": _VendorEndpoint("https://api.groq.com/openai/v1", CHAT_WIRE_API),
    "deepseek": _VendorEndpoint("https://api.deepseek.com", CHAT_WIRE_API),
    "xai": _VendorEndpoint("https://api.x.ai/v1", CHAT_WIRE_API),
    "mistral": _VendorEndpoint("https://api.mistral.ai/v1", CHAT_WIRE_API),
    "together_ai": _VendorEndpoint("https://api.together.xyz/v1", CHAT_WIRE_API),
    "fireworks_ai": _VendorEndpoint("https://api.fireworks.ai/inference/v1", CHAT_WIRE_API),
}


def key_provider_endpoint(provider: str) -> _VendorEndpoint | None:
    """Return the vendor endpoint + wire for a ``key`` *provider*, if non-default.

    :param provider: A catalog provider id, e.g. ``"openrouter"``.
    :returns: The :class:`_VendorEndpoint` (own base_url + ``wire_api``) for a
        third-party OpenAI-compatible vendor, or ``None`` for ``openai`` /
        ``anthropic`` (which use the canonical family base_url and, for
        openai, the Responses wire default).
    """
    return _KEY_PROVIDER_ENDPOINT.get(provider)


def build_key_provider_entry(
    family: str,
    base_url: str,
    api_key_ref: str,
    default_model: str | None,
    *,
    wire_api: str | None = None,
) -> dict[str, object]:
    """Build a ``kind: key`` provider entry body (config shape).

    :param family: The family this key serves, ``"anthropic"`` or
        ``"openai"``.
    :param base_url: The endpoint base URL, e.g.
        ``"https://api.anthropic.com"``.
    :param api_key_ref: The secret reference, e.g. ``"keychain:anthropic"``
        or ``"env:ANTHROPIC_API_KEY"``.
    :param default_model: The family default model id, e.g.
        ``"claude-sonnet-4-6"``, or ``None`` to omit the ``models`` block.
    :param wire_api: OpenAI wire protocol for an ``openai``-family key —
        ``"chat"`` for third-party vendors (OpenRouter, Groq, …) that only
        speak Chat Completions. Written onto the family block; ``None`` omits
        it (the executor defaults to the Responses API, correct for OpenAI).
    :returns: A provider entry body, e.g.
        ``{"kind": "key", "anthropic": {"base_url": "...",
        "api_key_ref": "keychain:anthropic",
        "models": {"default": "claude-sonnet-4-6"}}}``.
    """
    family_block: dict[str, object] = {"base_url": base_url, "api_key_ref": api_key_ref}
    if default_model:
        family_block["models"] = {"default": default_model}
    if wire_api is not None and family == OPENAI_FAMILY:
        family_block["wire_api"] = wire_api
    return {"kind": KEY_KIND, family: family_block}


def default_base_url_for_family(family: str) -> str:
    """Return the canonical vendor base URL for a ``key`` provider family.

    :param family: ``"anthropic"`` or ``"openai"``.
    :returns: The default endpoint base URL, e.g.
        ``"https://api.anthropic.com"`` (anthropic) or
        ``"https://api.openai.com/v1"`` (openai).
    :raises KeyError: If *family* is not a known family.
    """
    return _FAMILY_DEFAULT_BASE_URL[family]


def build_subscription_provider_entry(cli: str) -> dict[str, object]:
    """Build a ``kind: subscription`` provider entry body.

    :param cli: The CLI whose login carries auth, e.g. ``"claude"``.
    :returns: A provider entry body, e.g.
        ``{"kind": "subscription", "cli": "claude"}``.
    """
    return {"kind": SUBSCRIPTION_KIND, "cli": cli}


def build_gateway_provider_entry(
    base_url: str,
    api_key_ref: str,
    *,
    families: list[str],
    wire_api: str | None = None,
    models: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build a ``kind: gateway`` provider entry body (config shape).

    A gateway is an OpenAI/Anthropic-compatible proxy reached at a custom
    ``base_url`` (OpenRouter, LiteLLM, a local Ollama). It may serve the
    ``openai`` family, the ``anthropic`` family, or both — each family
    gets its own block pointing at the same base_url + key.

    :param base_url: The gateway base URL, e.g.
        ``"https://openrouter.ai/api/v1"``.
    :param api_key_ref: The secret reference, e.g.
        ``"keychain:openrouter"`` or ``"env:OPENROUTER_API_KEY"``.
    :param families: The families the gateway serves, a non-empty subset
        of ``["openai", "anthropic"]``.
    :param wire_api: Wire protocol for the **openai** family —
        ``"responses"`` (OpenAI / LiteLLM) or ``"chat"`` (OpenRouter and
        most OSS-model gateways, which don't implement the Responses API).
        Written only onto the ``openai`` block; the ``anthropic`` block
        (Messages API) has no wire choice. ``None`` omits it (the Responses
        API is then assumed). Getting this wrong is
        the usual reason an OpenRouter gateway fails while LiteLLM works.
    :param models: Optional per-family default model id, e.g.
        ``{"openai": "qwen/qwen3.7-plus"}``. Written as ``models.default``
        onto each named family's block — important for a gateway, which has
        no catalog default, so without it routing falls back to a vendor
        model the gateway may not host. ``None`` / a family absent from the
        map omits the pin for that family.
    :returns: A provider entry body, e.g.
        ``{"kind": "gateway", "openai": {"base_url": "...",
        "api_key_ref": "keychain:openrouter", "wire_api": "chat",
        "models": {"default": "qwen/qwen3.7-plus"}}}``.
    :raises ValueError: If *families* is empty.
    """
    if not families:
        raise ValueError("a gateway must serve at least one family")
    models = models or {}
    body: dict[str, object] = {"kind": GATEWAY_KIND}
    for family in families:
        block: dict[str, object] = {"base_url": base_url, "api_key_ref": api_key_ref}
        if family == OPENAI_FAMILY and wire_api is not None:
            block["wire_api"] = wire_api
        if models.get(family):
            block["models"] = {"default": models[family]}
        body[family] = block
    return body
