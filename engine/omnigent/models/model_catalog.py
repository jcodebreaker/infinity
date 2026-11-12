"""Deterministic model enumeration for sub-agent model awareness.

Backs the ``sys_list_models`` runner builtin: for each sub-agent worker
of an orchestrator's spec (plus the orchestrator brain itself), resolve
the model provider the spawn/launch paths would actually use — the same
precedence as :func:`omnigent.runtime.workflow._resolve_provider_for_build`
followed by the legacy auth fallthrough the spawn-env builders apply —
and enumerate that provider's live model listing. The resolved provider
*kind* is also what the ``sys_session_send`` dispatch gate consults for
canonical→gateway-local model-id normalization
(:func:`omnigent.models.model_override.normalize_model_for_provider`).

Enumeration is deterministic per provider kind:

- ``key`` with the ``anthropic`` family → ``GET <base_url>/v1/models``
  with ``x-api-key`` headers (source ``"anthropic-api"``).
- ``key`` (openai family) / ``gateway`` / ``local`` →
  ``GET <base_url>/v1/models`` with a bearer token (source
  ``"openai-compatible"``).
- ``subscription`` → curated static aliases for CLIs without a listing API
  (source ``"static"``, ``verified: false``).
- anything unresolvable → source ``"none"`` with an explanatory note,
  which doubles as a dead-worker preflight signal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, TypeAlias, cast
from urllib.parse import urlsplit

import click
import httpx
from cachetools import TTLCache

from omnigent._platform import default_shell_argv
from omnigent.harness_aliases import canonicalize_harness
from omnigent.llms.anthropic_model_metadata import parse_anthropic_model_metadata
from omnigent.models.model_metadata import (
    ModelCapability,
    ModelCostTier,
    ModelIntent,
    ModelMetadata,
)
from omnigent.models.model_override import is_codex_compatible_model, model_family_mismatch
from omnigent.models.model_resolver import (
    ModelResolution,
    ModelResolutionError,
    ModelResolutionRequest,
    resolve_model,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    BEDROCK_KIND,
    KEY_KIND,
    OPENAI_FAMILY,
    SUBSCRIPTION_KIND,
    ProviderEntry,
)
from omnigent.util.json_types import JsonObject as _JsonObject

if TYPE_CHECKING:
    from omnigent.onboarding.providers import ModelInfo
    from omnigent.spec.types import AgentSpec

_logger = logging.getLogger(__name__)

# Sentinel kind for "no usable provider resolved" rows.
NONE_KIND = "none"

# ~5 min: provider listings change rarely; a turn that fans out many
# dispatches should never re-fetch per call.
_CATALOG_TTL_S = 300.0
_HTTP_TIMEOUT_S = 10.0
_AUTH_COMMAND_TIMEOUT_S = 15.0

# Header version the Anthropic models API requires (same value as
# ``omnigent/llms/adapters/anthropic.py``).
_ANTHROPIC_API_VERSION = "2023-06-01"

_ProviderHarness: TypeAlias = Literal["claude-sdk", "codex", "pi"]

# Harness spellings -> the workflow harness whose provider resolution they
# share; natives resolve via their SDK sibling (the resolve_native_* rule).
_PROVIDER_RESOLUTION_HARNESS: dict[str, _ProviderHarness] = {
    "claude-sdk": "claude-sdk",
    "claude_sdk": "claude-sdk",
    "claude": "claude-sdk",
    "claude-native": "claude-sdk",
    "native-claude": "claude-sdk",
    "codex": "codex",
    "codex-native": "codex",
    "native-codex": "codex",
    "pi": "pi",
    "pi-native": "pi",
    "native-pi": "pi",
}

# Preferred inline family per single-family harness (pi consumes both).
_KEY_AUTH_FAMILY: dict[str, str] = {
    "claude-sdk": ANTHROPIC_FAMILY,
    "codex": OPENAI_FAMILY,
}

# Multi-family providers (pi): anthropic first, matching _apply_provider_to_pi.
_FAMILY_PREFERENCE = (ANTHROPIC_FAMILY, OPENAI_FAMILY)


@dataclass(frozen=True)
class ModelEntry:
    """One model a worker can run.

    :param id: Provider-local model id, e.g.
        ``"claude-sonnet-4-6"`` or ``"gpt-5.4-mini"``.
    :param family: Vendor family token — ``"claude"``, ``"openai"``, or
        ``"other"``.
    :param metadata: Provider-neutral capabilities and limits. Metadata fields
        remain unknown when the listing API reports only a model id.
    """

    id: str
    family: str
    metadata: ModelMetadata = field(default_factory=ModelMetadata)

    @property
    def context_window(self) -> int | None:
        """Return the normalized context window for compatibility callers."""
        return self.metadata.context_window


@dataclass(frozen=True)
class ModelListing:
    """A worker's enumerated model list plus its provenance.

    :param source: Where the list came from — ``"gateway"``,
        ``"openai-compatible"``, ``"anthropic-api"``, ``"cli"``,
        ``"static"``, or ``"none"``.
    :param verified: ``True`` when the list was fetched live from the
        provider; ``False`` for static/curated or empty listings.
    :param models: The enumerated models, e.g.
        ``(ModelEntry(id="gpt-5.4", family="openai"),)``.
    :param note: Human-readable provenance / failure explanation.
    """

    source: str
    verified: bool
    models: tuple[ModelEntry, ...]
    note: str


@dataclass(frozen=True)
class ResolvedModelProvider:
    """The model provider a worker's spawn/launch path would route through.

    :param kind: Provider kind — ``"key"`` / ``"gateway"`` / ``"local"``
        / ``"subscription"`` from
        the provider config layer, or ``"none"`` when no usable provider
        resolved.
    :param family: ``"anthropic"`` / ``"openai"`` for inline-family
        kinds, else ``None``.
    :param base_url: Endpoint base URL for inline-family kinds, e.g.
        ``"https://openrouter.ai/api/v1"``.
    :param api_key: Resolved static credential for inline-family kinds.
        Never serialized into tool output.
    :param auth_command: Shell command printing a bearer token, for
        providers configured with a dynamic credential.
    :param cli: ``"claude"`` / ``"pi"`` for ``kind="subscription"``.
    :param detail: Non-secret descriptor of how the provider resolved,
        e.g. ``"provider 'openrouter'"`` — used in listing notes.
    """

    kind: str
    family: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    auth_command: str | None = None
    cli: str | None = None
    detail: str = ""


def _model_configuration_host(base_url: str) -> str | None:
    """Extract a host without carrying URL userinfo into browser metadata."""
    try:
        parsed = urlsplit(base_url if "://" in base_url else f"//{base_url}")
        if not parsed.hostname:
            return None
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    return f"{hostname}:{port}" if port is not None else hostname


def model_configuration_source(
    provider: ResolvedModelProvider, *, harness: str | None = None
) -> dict[str, str] | None:
    """Return non-secret coordinates describing how a model is reached."""
    if provider.kind == NONE_KIND:
        return None

    # Native Claude deliberately ignores legacy api_key auth and uses its own
    # CLI login. Named key providers still route through the configured key.
    if (
        harness in {"claude-native", "native-claude"}
        and provider.kind == KEY_KIND
        and provider.detail == "api_key auth"
    ):
        return {"kind": SUBSCRIPTION_KIND, "label": "Subscription", "name": "claude"}

    source: dict[str, str] = {"kind": provider.kind}
    if provider.kind == SUBSCRIPTION_KIND:
        source.update(label="Subscription", name=provider.cli or "CLI login")
    elif provider.kind in {"gateway", "local"}:
        source["label"] = "AI Gateway" if provider.kind == "gateway" else "Local"
        name = provider.detail.removeprefix("provider '").removesuffix("'")
        if name:
            source["name"] = name
    elif provider.kind == BEDROCK_KIND:
        source["label"] = "Bedrock"
        name = provider.detail.removeprefix("provider '").removesuffix("'")
        if name:
            source["name"] = name
    else:
        source["label"] = "API key"
        name = provider.family or provider.detail.removeprefix("provider '").removesuffix("'")
        if name:
            source["name"] = name
    if provider.base_url and (host := _model_configuration_host(provider.base_url)):
        source["host"] = host
    return source


def is_direct_openai_provider(provider: ResolvedModelProvider) -> bool:
    """Return whether *provider* targets OpenAI's canonical models API."""
    if provider.family != OPENAI_FAMILY or not provider.base_url:
        return False
    from omnigent.onboarding.provider_config import default_base_url_for_family

    canonical = default_base_url_for_family(OPENAI_FAMILY)
    return _models_url(provider.base_url).lower() == _models_url(canonical).lower()


# Unfiltered listings keyed by provider identity. TTLCache is not thread-safe
# and enumeration runs via asyncio.to_thread, so accesses lock; the HTTP fetch
# stays outside it (duplicate fetches are benign, corruption is not).
_listing_cache: TTLCache[tuple[str, ...], ModelListing] = TTLCache(maxsize=64, ttl=_CATALOG_TTL_S)
_listing_cache_lock = threading.Lock()


def _credential_fingerprint(provider: ResolvedModelProvider) -> str:
    """Non-secret identity of the provider's credential for cache keying.

    Two providers sharing kind + base_url but holding different
    credentials may see different listings, so the cache key must carry
    credential identity without ever storing the secret itself.

    :param provider: The resolved provider descriptor.
    :returns: A sha256-prefix hex digest of the resolved credential (or
        ``auth_command`` string), e.g. ``"9f86d081884c7d65"``; ``""``
        when the provider carries no inline credential.
    """
    if provider.api_key:
        material = f"key:{provider.api_key}"
    elif provider.auth_command:
        material = f"cmd:{provider.auth_command}"
    else:
        return ""
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _listing_cache_key(provider: ResolvedModelProvider) -> tuple[str, ...]:
    """Cache identity for one provider's unfiltered listing.

    Carries the full provider coordinates — kind, family,
    base URL, CLI, the non-secret ``detail`` (provider name), and a
    credential fingerprint — so distinct providers never replay each
    other's listings.

    :param provider: The resolved provider descriptor.
    :returns: A hashable tuple of non-secret identity strings.
    """
    return (
        provider.kind,
        provider.family or "",
        provider.base_url or "",
        provider.cli or "",
        provider.detail,
        _credential_fingerprint(provider),
    )


def clear_model_catalog_cache() -> None:
    """Drop every cached provider listing.

    Listings are cached per provider identity for
    :data:`_CATALOG_TTL_S`; call this after reconfiguring providers (or
    between tests) to force a fresh fetch.
    """
    with _listing_cache_lock:
        _listing_cache.clear()


def model_family_token(model_id: str) -> str:
    """Tag a model id with the harness family that can serve it.

    Shares the token rule with
    :func:`omnigent.models.model_override.model_family_mismatch`: Claude ids
    contain ``"claude"``; the ``"openai"`` token covers every
    codex-compatible id (gpt/codex plus the GLM and Kimi families, which
    serve on the same Responses wire).

    :param model_id: Model id, e.g. ``"claude-opus-4-8"``.
    :returns: ``"claude"``, ``"openai"``, or ``"other"``.
    """
    if "claude" in model_id.lower():
        return "claude"
    if is_codex_compatible_model(model_id):
        return "openai"
    return "other"


def model_family_conflict(harness: str, model_id: str) -> str | None:
    """Return a rejection reason when *model_id* is recognizably another vendor's model.

    :func:`~omnigent.models.model_override.model_family_mismatch` also rejects ids whose family
    cannot be told (the right call for a sub-agent dispatch). Operator catalogs name gateway
    aliases (``gateway/fast``) that only resolve later, so a configured or listed model is
    rejected only when its family token says Claude or GPT/GLM/Kimi and the harness cannot run it.

    :returns: The mismatch reason, or ``None`` when compatible or when the family is unknown.
    """
    reason = model_family_mismatch(harness, model_id)
    return reason if reason and model_family_token(model_id) != "other" else None


def catalog_model_entries(provider_name: str) -> tuple[ModelEntry, ...]:
    """Load provider chat models as normalized resolver candidates.

    The upstream MLflow catalog is fetched and cached by
    :func:`omnigent.onboarding.providers.get_chat_models`. This adapter keeps
    provider discovery separate from selection while preserving catalog order
    as the resolver's deterministic tie-breaker.

    :param provider_name: MLflow catalog provider, e.g. ``"openai"`` or
        ``"anthropic"``.
    :returns: Normalized model entries, newest/preferred catalog entries first.
    """
    from omnigent.onboarding.providers import get_chat_models

    models = get_chat_models(provider_name)
    cost_tiers = _catalog_cost_tiers(models)
    entries: list[ModelEntry] = []
    for index, model in enumerate(models):
        supported: set[ModelCapability] = set()
        unsupported: set[ModelCapability] = set()
        for capability, value in (
            (ModelCapability.TOOL_USE, model.supports_function_calling),
            (ModelCapability.REASONING, model.supports_reasoning),
            (ModelCapability.VISION, model.supports_vision),
            (ModelCapability.STRUCTURED_OUTPUT, model.supports_structured_output),
        ):
            if value is True:
                supported.add(capability)
            elif value is False:
                unsupported.add(capability)
        entries.append(
            ModelEntry(
                id=model.name,
                family=model_family_token(model.name),
                metadata=ModelMetadata(
                    supported_capabilities=frozenset(supported),
                    unsupported_capabilities=frozenset(unsupported),
                    context_window=model.max_input_tokens,
                    max_output_tokens=model.max_output_tokens,
                    cost_tier=cost_tiers.get(index),
                ),
            )
        )
    return tuple(entries)


def resolve_catalog_model(
    provider_name: str,
    *,
    intent: ModelIntent = ModelIntent.DEFAULT,
    configured_default: str | None = None,
    family: str | None = None,
) -> ModelResolution:
    """Resolve a model from the live bundled provider catalog.

    Default intent preserves the onboarding provider's general-purpose model
    policy after family and gateway-routing constraints are applied. Other
    intents continue to rank all compatible catalog candidates by metadata.

    :param provider_name: MLflow catalog provider name.
    :param intent: Stable selection intent.
    :param configured_default: Optional provider-configured model. It is
        represented as a candidate in *family* so provider configuration keeps
        precedence even when the remote catalog has not learned the id yet.
    :param family: Optional normalized catalog family (``"claude"`` /
        ``"openai"`` / ``"other"``).
    :returns: The resolver result with source and normalized metadata.
    :raises ModelResolutionError: When no compatible catalog model exists.
    """
    from omnigent.onboarding.providers import default_chat_model

    models = list(catalog_model_entries(provider_name))

    if configured_default is None and intent == ModelIntent.DEFAULT:
        compatible_ids = {model.id for model in models if family is None or model.family == family}
        preferred_default = default_chat_model(
            provider_name,
            allowed_models=compatible_ids,
        )
        if preferred_default is not None and (
            family is None or model_family_token(preferred_default) == family
        ):
            configured_default = preferred_default

    if configured_default is not None and all(model.id != configured_default for model in models):
        models.insert(
            0,
            ModelEntry(
                id=configured_default,
                family=family or model_family_token(configured_default),
            ),
        )
    try:
        return resolve_model(
            ModelResolutionRequest(
                intent=intent,
                configured_default=configured_default,
                allowed_families=(frozenset({family}) if family is not None else frozenset()),
            ),
            models,
        )
    except ModelResolutionError as exc:
        family_detail = f" for family {family!r}" if family is not None else ""
        raise ModelResolutionError(
            f"no compatible model resolved from provider {provider_name!r}{family_detail}; "
            "configure an explicit model or retry when catalog discovery is available"
        ) from exc


def _catalog_cost_tiers(models: list[ModelInfo]) -> dict[int, ModelCostTier]:
    """Assign provider-relative thirds from reported token prices."""
    priced: list[tuple[int, float]] = []
    for index, model in enumerate(models):
        input_price = getattr(model, "input_price", None)
        output_price = getattr(model, "output_price", None)
        if isinstance(input_price, (int, float)) and isinstance(output_price, (int, float)):
            priced.append((index, float(input_price) + float(output_price)))
    distinct = sorted({price for _, price in priced})
    if not distinct:
        return {}
    if len(distinct) == 1:
        return {index: ModelCostTier.STANDARD for index, _ in priced}
    tiers: dict[int, ModelCostTier] = {}
    price_rank = {price: rank / (len(distinct) - 1) for rank, price in enumerate(distinct)}
    for index, price in priced:
        percentile = price_rank[price]
        if percentile <= 1 / 3:
            tiers[index] = ModelCostTier.ECONOMY
        elif percentile >= 2 / 3:
            tiers[index] = ModelCostTier.PREMIUM
        else:
            tiers[index] = ModelCostTier.STANDARD
    return tiers


def spec_harness(spec: object) -> str | None:
    """Resolve the declared harness for a (sub-)agent spec.

    Mirrors the runner's harness derivation
    (``executor.config["harness"]`` falling back to ``executor.type``)
    with defensive attribute access so structural spec stubs degrade to
    ``None`` instead of raising.

    :param spec: An :class:`AgentSpec` (or structural equivalent).
    :returns: Harness id, e.g. ``"codex-native"``, or ``None``.
    """
    executor = getattr(spec, "executor", None)
    if executor is None:
        return None
    config = getattr(executor, "config", None)
    harness = config.get("harness") if isinstance(config, dict) else None
    if isinstance(harness, str) and harness:
        return harness
    executor_type = getattr(executor, "type", None)
    return executor_type if isinstance(executor_type, str) and executor_type else None


def resolve_model_provider(spec: object, harness: str | None) -> ResolvedModelProvider:
    """Resolve the model provider a worker's launch path would use.

    Total by contract: callers (the dispatch gate and ``sys_list_models``)
    must never crash on a malformed spec or broken provider config, so
    any resolution failure collapses to ``kind="none"`` — the gate then
    passes the model through unchanged and the tool reports the failure.

    :param spec: The worker's (sub-)agent spec.
    :param harness: The worker's harness id, e.g. ``"claude-native"``.
    :returns: A :class:`ResolvedModelProvider`; ``kind="none"`` when the
        provider cannot be determined.
    """
    try:
        return _resolve_model_provider_unsafe(spec, harness)
    except Exception as exc:
        from omnigent.errors import OmnigentError

        _logger.debug("model provider resolution failed for harness %r", harness, exc_info=True)
        # OmnigentError text is this codebase's own (secret-free); anything
        # else is redacted to its type name — raw detail stays at DEBUG.
        reason = str(exc) if isinstance(exc, OmnigentError) else type(exc).__name__
        return ResolvedModelProvider(
            kind=NONE_KIND, detail=f"provider resolution failed: {reason}"
        )


def _resolve_model_provider_unsafe(spec: object, harness: str | None) -> ResolvedModelProvider:
    """Resolve the provider, propagating failures to the catch-all wrapper.

    Step 1 reuses :func:`~omnigent.runtime.workflow._resolve_provider_for_build`
    verbatim (the precedence the spawn-env builders and native launch
    paths share). Step 2 mirrors the builders' PER-HARNESS legacy
    fallthrough (see :func:`_provider_from_legacy_auth`) — the builders
    diverge in which legacy auth fields they actually consume.

    :param spec: The worker's (sub-)agent spec.
    :param harness: The worker's harness id, e.g. ``"pi"``.
    :returns: A :class:`ResolvedModelProvider`.
    """
    # Imported lazily; workflow.py imports broadly and this module is
    # consumed from the runner's dispatch path.
    from omnigent.runtime.workflow import _resolve_provider_for_build

    canonical_harness = canonicalize_harness(harness) or harness
    harness_type = _PROVIDER_RESOLUTION_HARNESS.get(canonical_harness or "")
    if harness_type is None:
        return ResolvedModelProvider(
            kind=NONE_KIND,
            detail=f"harness {harness or 'unknown'!r} has no model-provider resolution",
        )

    agent_spec = cast("AgentSpec", spec)
    entry = _resolve_provider_for_build(
        agent_spec, harness_type=harness_type, actual_harness=harness
    )
    if entry is not None:
        return _provider_from_entry(entry, harness_type)
    return _provider_from_legacy_auth(agent_spec, harness_type)


def _provider_from_legacy_auth(
    spec: AgentSpec, harness_type: _ProviderHarness
) -> ResolvedModelProvider:
    """Mirror the per-harness legacy fallthrough of ``_build_*_spawn_env``.

    Only the claude-sdk builder consumes spec/global ``api_key`` ``auth:``
    blocks; the codex and pi builders never read them, so reporting one as
    usable would list models the spawned child cannot actually reach.

    :param spec: The worker's (sub-)agent spec.
    :param harness_type: The workflow harness type, e.g. ``"codex"``.
    :returns: A :class:`ResolvedModelProvider`.
    """
    if harness_type == "claude-sdk":
        return _legacy_claude_sdk_provider(spec)
    if spec.executor.auth is not None:
        return ResolvedModelProvider(
            kind=NONE_KIND,
            detail=(
                f"the {harness_type} spawn path does not consume legacy auth: "
                "fields; configure a 'providers:' entry instead"
            ),
        )
    return ResolvedModelProvider(kind=NONE_KIND, detail="no model provider configured")


def _legacy_claude_sdk_provider(spec: AgentSpec) -> ResolvedModelProvider:
    """Mirror ``_build_claude_sdk_spawn_env``'s legacy auth branch.

    Spec ``auth:`` (api_key) → global ``auth:`` → none. The api_key path
    routes via ``apiKeyHelper`` to the vendor API, so ``auth.base_url`` is
    NOT consumed — listings use the vendor default.

    :param spec: The worker's (sub-)agent spec.
    :returns: A :class:`ResolvedModelProvider`.
    """
    from omnigent.onboarding.provider_config import default_base_url_for_family
    from omnigent.runtime.workflow import _load_global_auth
    from omnigent.spec.types import ApiKeyAuth

    auth = spec.executor.auth
    if auth is None:
        auth = _load_global_auth()
    if isinstance(auth, ApiKeyAuth) and auth.api_key:
        return ResolvedModelProvider(
            kind=KEY_KIND,
            family=ANTHROPIC_FAMILY,
            base_url=default_base_url_for_family(ANTHROPIC_FAMILY),
            api_key=auth.api_key,
            detail="api_key auth",
        )
    return ResolvedModelProvider(kind=NONE_KIND, detail="no model provider configured")


def _provider_from_entry(entry: ProviderEntry, harness_type: str) -> ResolvedModelProvider:
    """Map a resolved :class:`ProviderEntry` to a provider descriptor.

    :param entry: The provider entry resolved for the worker.
    :param harness_type: The workflow harness type, e.g. ``"codex"``.
    :returns: A :class:`ResolvedModelProvider`; ``kind="none"`` when an
        inline-family provider has no usable family for the harness.
    """
    from omnigent.errors import OmnigentError

    if entry.kind == SUBSCRIPTION_KIND:
        return ResolvedModelProvider(
            kind=SUBSCRIPTION_KIND, cli=entry.cli, detail=f"provider {entry.name!r}"
        )
    # Inline-family kinds: single-family harnesses get exactly their family;
    # pi takes the first whose credential resolves, anthropic preferred.
    preferred = None if harness_type == "pi" else _KEY_AUTH_FAMILY[harness_type]
    candidates = (preferred,) if preferred is not None else _FAMILY_PREFERENCE
    for family_name in candidates:
        try:
            family = entry.family(family_name)
        except OmnigentError:
            # Credential unset/unresolvable: skip (the pi optional-family rule).
            continue
        if family is None:
            continue
        return ResolvedModelProvider(
            kind=entry.kind,
            family=family_name,
            base_url=family.base_url,
            api_key=family.api_key,
            auth_command=family.auth_command,
            detail=f"provider {entry.name!r}",
        )
    return ResolvedModelProvider(
        kind=NONE_KIND,
        detail=(
            f"provider {entry.name!r} configures no family with resolvable "
            f"credentials for this harness"
        ),
    )


def list_models_for_worker(
    spec: object,
    harness: str | None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ModelListing:
    """Enumerate the models one worker can run, family-filtered.

    Resolves the worker's provider, fetches (or replays from the TTL
    cache) its unfiltered model listing, then applies the harness's
    family rule from :func:`~omnigent.models.model_override.model_family_mismatch`
    — claude harnesses keep Claude ids, codex harnesses keep GPT ids,
    pi keeps everything.

    :param spec: The worker's (sub-)agent spec.
    :param harness: The worker's harness id, e.g. ``"codex-native"``.
    :param transport: Optional httpx transport override so tests mock at
        the HTTP boundary; ``None`` uses the default transport.
    :returns: The worker's :class:`ModelListing`.
    """
    provider = resolve_model_provider(spec, harness)
    listing = _listing_for_provider(provider, transport=transport)
    if harness is None:
        return listing
    from omnigent.inference_config import binding_for_harness, load_runtime_inference_config

    binding = binding_for_harness(load_runtime_inference_config(), harness)
    if binding is not None:
        if binding.model_allowlist is None:
            return listing
        by_id = {model.id: model for model in listing.models}
        return replace(
            listing,
            models=tuple(by_id[mid] for mid in binding.model_allowlist if mid in by_id),
        )
    filtered = tuple(m for m in listing.models if model_family_mismatch(harness, m.id) is None)
    return replace(listing, models=filtered)


def catalog_for_spec(
    spec: object,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, _JsonObject]:
    """Build the full ``sys_list_models`` payload for an agent spec.

    One row per declared sub-agent, keyed by sub-agent name, plus a
    ``"self"`` row for the calling agent's own (brain) harness. Failures
    are isolated per worker: one broken provider yields a ``"none"`` row
    with the failure in its note and never hides the other workers.

    :param spec: The calling agent's spec (sub-agents enumerated from
        ``spec.sub_agents``).
    :param transport: Optional httpx transport override for tests.
    :returns: Mapping of worker name → row dict with ``source`` /
        ``verified`` / ``models`` / ``note`` keys.
    """
    rows: dict[str, _JsonObject] = {}
    for sub in getattr(spec, "sub_agents", None) or []:
        name = getattr(sub, "name", None)
        if not isinstance(name, str) or not name:
            continue
        rows[name] = _worker_row(sub, transport=transport)
    rows["self"] = _worker_row(spec, transport=transport)
    return rows


def _worker_row(
    spec: object,
    *,
    transport: httpx.BaseTransport | None,
) -> _JsonObject:
    """Build one worker's catalog row, never raising.

    :param spec: The worker's (sub-)agent spec.
    :param transport: Optional httpx transport override for tests.
    :returns: Row dict with ``source`` / ``verified`` / ``models`` /
        ``note`` keys.
    """
    harness = spec_harness(spec)
    try:
        listing = list_models_for_worker(spec, harness, transport=transport)
    except Exception as exc:
        _logger.debug("worker model enumeration failed", exc_info=True)
        listing = ModelListing(
            source=NONE_KIND,
            verified=False,
            models=(),
            note=f"model enumeration failed: {_redacted_failure_reason(exc)}",
        )
    return _listing_payload(listing)


def _listing_payload(listing: ModelListing) -> _JsonObject:
    """Serialize a :class:`ModelListing` into the tool's JSON row shape.

    :param listing: The listing to serialize.
    :returns: Row dict; ``context_window`` appears only when known.
    """
    models: list[_JsonObject] = []
    for entry in listing.models:
        row: _JsonObject = {"id": entry.id, "family": entry.family}
        metadata = entry.metadata
        if metadata.context_window is not None:
            row["context_window"] = metadata.context_window
        if metadata.max_output_tokens is not None:
            row["max_output_tokens"] = metadata.max_output_tokens
        capabilities = {
            capability.value: supported
            for supported, values in (
                (True, metadata.supported_capabilities),
                (False, metadata.unsupported_capabilities),
            )
            for capability in sorted(values, key=lambda value: value.value)
        }
        if capabilities:
            row["capabilities"] = capabilities
        if metadata.cost_tier is not None:
            row["cost_tier"] = metadata.cost_tier.value
        if metadata.wire_apis:
            row["wire_apis"] = sorted(wire_api.value for wire_api in metadata.wire_apis)
        if metadata.reasoning is not None:
            row["reasoning"] = {
                "modes": sorted(mode.value for mode in metadata.reasoning.modes),
                "efforts": sorted(metadata.reasoning.efforts),
            }
        models.append(row)
    payload: _JsonObject = {
        "source": listing.source,
        "verified": listing.verified,
        "models": models,
        "note": listing.note,
    }
    return payload


def _redacted_failure_reason(exc: Exception) -> str:
    """Map an enumeration failure to a secret-free note category.

    Raw exception text can embed secrets — ``CalledProcessError`` /
    ``TimeoutExpired`` stringify the full ``auth_command`` — and the
    note flows into ``sys_list_models`` output (LLM-visible, persisted
    in the transcript). Callers log the raw exception at DEBUG.

    :param exc: The enumeration failure.
    :returns: A redacted human-readable category, e.g.
        ``"listing endpoint returned HTTP 503"``.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return "provider auth command timed out"
    if isinstance(exc, subprocess.SubprocessError):
        return "provider auth command failed"
    if isinstance(exc, click.ClickException):
        return exc.message
    if isinstance(exc, httpx.HTTPStatusError):
        return f"listing endpoint returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "listing endpoint unreachable"
    if isinstance(exc, json.JSONDecodeError):
        return "listing endpoint returned malformed JSON"
    if isinstance(exc, ValueError):
        # The remaining ValueErrors are this module's own static,
        # secret-free messages (no credential / no base_url / empty token).
        return str(exc)
    if isinstance(exc, OSError):
        return "provider credentials or network unavailable"
    return type(exc).__name__


def listing_for_provider(provider: ResolvedModelProvider) -> ModelListing:
    """Enumerate one provider's model listing (cached, failures not cached).

    The public face of :func:`_listing_for_provider` for callers that already
    hold a :class:`ResolvedModelProvider` — e.g. a harness executor asking
    what its gateway transport serves — rather than an agent spec.

    :param provider: The resolved provider descriptor.
    :returns: The provider's :class:`ModelListing`; ``verified`` is ``False``
        with a ``note`` when the fetch failed.
    """
    return _listing_for_provider(provider, transport=None)


def _listing_for_provider(
    provider: ResolvedModelProvider,
    *,
    transport: httpx.BaseTransport | None,
) -> ModelListing:
    """Enumerate (or replay from cache) one provider's unfiltered listing.

    Live fetches are cached for :data:`_CATALOG_TTL_S` keyed by provider
    identity; failures are returned (not cached) so a transient outage
    retries on the next call.

    :param provider: The resolved provider descriptor.
    :param transport: Optional httpx transport override for tests.
    :returns: The provider's :class:`ModelListing`.
    """
    if provider.kind == NONE_KIND:
        return ModelListing(
            source=NONE_KIND,
            verified=False,
            models=(),
            note=(
                f"no usable model provider ({provider.detail}) — dispatches to "
                "this worker cannot run here"
            ),
        )
    if provider.kind == SUBSCRIPTION_KIND:
        return _static_subscription_listing(provider)

    cache_key = _listing_cache_key(provider)
    with _listing_cache_lock:
        cached = cast(ModelListing | None, _listing_cache.get(cache_key))
    if cached is not None:
        return cached
    try:
        if provider.kind == KEY_KIND and provider.family == ANTHROPIC_FAMILY:
            listing = _fetch_anthropic_listing(provider, transport=transport)
        else:
            listing = _fetch_openai_compatible_listing(provider, transport=transport)
    except (
        click.ClickException,
        httpx.HTTPError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        _logger.debug(
            "model enumeration failed for %s", provider.detail or provider.kind, exc_info=True
        )
        return ModelListing(
            source=NONE_KIND,
            verified=False,
            models=(),
            note=(
                f"model enumeration failed for {provider.detail or provider.kind}: "
                f"{_redacted_failure_reason(exc)}"
            ),
        )
    with _listing_cache_lock:
        _listing_cache[cache_key] = listing
    return listing


def _static_subscription_listing(provider: ResolvedModelProvider) -> ModelListing:
    """Build the (empty) pre-launch listing for a subscription CLI login.

    Subscription logins expose no model-listing API, and the curated
    stand-ins this used to serve are gone — the live harness probes are the
    source of truth, so a path that cannot probe reports nothing rather
    than a plausible-but-stale list.

    :param provider: A ``kind="subscription"`` provider descriptor.
    :returns: A ``source="static"`` listing with no models.
    """
    return ModelListing(
        source="static",
        verified=False,
        models=(),
        note=(
            f"the {provider.cli or 'unknown'} CLI login exposes no model-listing "
            "API before launch; the live listing comes from probing the harness"
        ),
    )


# A value no endpoint will honor, so the request trips the output-token
# validator before the model generates anything.
_PROBE_OUTPUT_TOKENS = 100_000_000

# Serving endpoints report an exceeded output limit in one of two
# shapes: "max_tokens (N) cannot exceed CAP" or "max_new_tokens N cannot be
# greater than max_output_tokens CAP". Capture CAP from either.
_OUTPUT_CAP_RE = re.compile(r"cannot exceed (\d+)|max_output_tokens\D*?(\d+)")


def probe_output_token_cap(
    base_url: str,
    token: str,
    model_id: str,
    *,
    api_type: str = "openai-completions",
    transport: httpx.BaseTransport | None = None,
) -> int | None:
    """Discover a serving endpoint's enforced per-request output-token cap.

    The Unity Catalog model-services listing carries no token limits, and a
    model's native ceiling (its catalog ``max_output_tokens``) can exceed what
    the serving endpoint accepts — requesting the native value then
    fails at runtime. Sending an oversized request trips the endpoint's
    validator, which names the real cap in its 400 body. The model never
    generates, so this is a single cheap round-trip that also tracks any future
    increase to the cap.

    :param base_url: Surface base, e.g. ``https://ws/ai-gateway/mlflow/v1``.
    :param token: Workspace bearer token.
    :param model_id: Served model id, e.g. ``system.ai.kimi-k3``.
    :param api_type: ``"openai-responses"`` or ``"openai-completions"`` —
        selects the request shape and path.
    :param transport: Optional httpx transport override for tests.
    :returns: The cap in tokens, or ``None`` when the endpoint accepts the
        probe (no cap below it) or the limit cannot be read (network/parse
        failure). ``None`` means "keep the catalog value".
    """
    if api_type == "openai-responses":
        path = "/responses"
        body: dict[str, object] = {
            "model": model_id,
            "input": "cap probe",
            "max_output_tokens": _PROBE_OUTPUT_TOKENS,
        }
    else:
        path = "/chat/completions"
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": "cap probe"}],
            "max_tokens": _PROBE_OUTPUT_TOKENS,
        }
    try:
        with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
    except httpx.HTTPError:
        return None
    if resp.status_code < 400:
        return None
    match = _OUTPUT_CAP_RE.search(resp.text or "")
    if match is None:
        return None
    cap = match.group(1) or match.group(2)
    return int(cap) if cap else None


def _models_url(base_url: str) -> str:
    """Derive the model-listing URL from a provider base URL.

    :param base_url: Endpoint base URL, e.g.
        ``"https://openrouter.ai/api/v1"`` or
        ``"https://api.anthropic.com"``.
    :returns: The listing URL — ``<base>/models`` when the base already
        ends in a version segment (``/v1``, ``/v4``, …), else
        ``<base>/v1/models``.
    """
    trimmed = base_url.rstrip("/")
    # A base that already names its API version lists at <base>/models;
    # appending /v1 again builds a path no server serves.
    if re.search(r"/v[0-9]+\Z", urlsplit(trimmed).path):
        return f"{trimmed}/models"
    return f"{trimmed}/v1/models"


def _resolve_bearer_token(provider: ResolvedModelProvider) -> str:
    """Resolve the provider's credential to a bearer-token string.

    :param provider: An inline-family provider descriptor.
    :returns: The token, e.g. ``"sk-or-..."``.
    :raises ValueError: When the provider carries no credential or its
        ``auth_command`` prints nothing.
    :raises subprocess.SubprocessError: When the ``auth_command`` fails.
    """
    if provider.api_key:
        return provider.api_key
    if provider.auth_command:
        # Same trust model as the harness executors, which run the
        # user-configured auth_command to mint gateway tokens.
        result = subprocess.run(
            default_shell_argv(provider.auth_command),
            capture_output=True,
            text=True,
            timeout=_AUTH_COMMAND_TIMEOUT_S,
            check=True,
        )
        token = result.stdout.strip()
        if not token:
            raise ValueError("provider auth_command printed no token")
        return token
    raise ValueError("provider has no credential to list models with")


def _fetch_openai_compatible_listing(
    provider: ResolvedModelProvider,
    *,
    transport: httpx.BaseTransport | None,
) -> ModelListing:
    """List models from an OpenAI-compatible ``/v1/models`` endpoint.

    :param provider: An inline-family provider descriptor (an OpenAI key
        or an OpenRouter/LiteLLM-style gateway/local endpoint).
    :param transport: Optional httpx transport override for tests.
    :returns: A ``source="openai-compatible"`` listing; entries carry
        ``context_window`` when the endpoint reports ``context_length``.
    :raises ValueError: When the provider has no base URL or credential.
    :raises httpx.HTTPError: On transport/HTTP failures.
    """
    if not provider.base_url:
        raise ValueError("provider has no base_url to list models from")
    token = _resolve_bearer_token(provider)
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.get(
            _models_url(provider.base_url),
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        payload = resp.json()
    models: list[ModelEntry] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        context_length = item.get("context_length")
        models.append(
            ModelEntry(
                id=model_id,
                family=model_family_token(model_id),
                metadata=ModelMetadata(
                    context_window=context_length if isinstance(context_length, int) else None
                ),
            )
        )
    return ModelListing(
        source="openai-compatible",
        verified=True,
        models=tuple(models),
        note=f"models reported by {_models_url(provider.base_url)}",
    )


def _fetch_anthropic_listing(
    provider: ResolvedModelProvider,
    *,
    transport: httpx.BaseTransport | None,
) -> ModelListing:
    """List models from the Anthropic models API (real keys only).

    :param provider: A ``kind="key"`` anthropic-family descriptor.
    :param transport: Optional httpx transport override for tests.
    :returns: A ``source="anthropic-api"`` listing.
    :raises ValueError: When the provider has no base URL or credential.
    :raises httpx.HTTPError: On transport/HTTP failures (subscription
        OAuth tokens are rejected here — only real API keys work).
    """
    if not provider.base_url:
        raise ValueError("provider has no base_url to list models from")
    token = _resolve_bearer_token(provider)
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.get(
            _models_url(provider.base_url),
            headers={"x-api-key": token, "anthropic-version": _ANTHROPIC_API_VERSION},
        )
        resp.raise_for_status()
        payload = resp.json()
    models: list[ModelEntry] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        models.append(
            ModelEntry(
                id=model_id,
                family=model_family_token(model_id),
                metadata=parse_anthropic_model_metadata(item),
            )
        )
    return ModelListing(
        source="anthropic-api",
        verified=True,
        models=tuple(models),
        note=f"models reported by {_models_url(provider.base_url)}",
    )
