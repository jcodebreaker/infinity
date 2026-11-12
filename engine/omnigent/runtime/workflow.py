"""Agent execution workflow — the core agent loop.

Load agent → build prompt → call LLM → execute tools → repeat.
All durably checkpointed for crash recovery.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

if TYPE_CHECKING:
    # Lazy: PolicyEngine + PolicyResult are pulled in at runtime via
    # the function-local imports inside ``_handle_policy_ask`` and
    # ``_enforce_policy``-using helpers (see uses below). The
    # TYPE_CHECKING import is only here so static type-checkers + ruff
    # can resolve the names in deferred annotations (``from __future__
    # import annotations`` is in effect).
    from omnigent.harnesses.datamodel import OSEnvSpec

from omnigent.entities import (
    NON_CONTENT_ITEM_TYPES,
    CompactionData,
    ConversationItem,
    NewConversationItem,
)
from omnigent.errors import (
    ErrorCode,
    OmnigentError,
    StaleCursorError,
    restart_on_stale_cursor,
)
from omnigent.llms import Client as LLMClient
from omnigent.models.model_catalog import resolve_catalog_model
from omnigent.models.model_resolver import ModelResolutionError
from omnigent.onboarding.detected import (
    effective_config_with_detected,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    BEDROCK_KIND,
    OPENAI_FAMILY,
    RESPONSES_WIRE_API,
    SUBSCRIPTION_KIND,
    FamilyConfig,
    ProviderEntry,
    default_provider_for_harness,
    first_available_provider,
    harness_family,
    load_config,
    load_providers,
)
from omnigent.runtime import (
    get_artifact_store,
    get_conversation_store,
    get_file_store,
)
from omnigent.runtime.compaction import (
    CompactionResult,
    SummaryMetadata,
    _CompactionState,
    compact,
    compaction_to_history_items,
    count_tokens,
)
from omnigent.runtime.content_resolver import resolve_content_references
from omnigent.runtime.prompt import build_instructions, history_to_input_items
from omnigent.spec import AgentSpec
from omnigent.spec.parser import check_unresolved_env_vars
from omnigent.spec.types import (
    ApiKeyAuth,
    LLMConfig,
    ProviderAuth,
    RetryPolicy,
)
from omnigent.stores import ConversationStore
from omnigent.util.env_credentials import expand_envvars_with_omnigent_prefix

# ── Module-level constants ────────────────────────────────────

_logger = logging.getLogger(__name__)

# Task kind for background `@tool(synchronous=False)` work items —
# the unit the parent loop separates from the polling-based
# sub-agent path so each kind uses the right collection mechanism.
# Kinds whose completion arrives via the async_work_complete drain
# and that can block the parent turn from finalizing. Tools,
# sub-agents, and client tools belong here: they represent jobs the
# parent expects to finish, and blocking the turn so the LLM sees
# the result in-line is a real UX win.

# Per-payload character cap for sub-agent output piggy-backed on
# the async_work_complete signal (matches the @tool path's
# ``truncate_for_llm`` budget — keeps the LLM-facing system
# message under control regardless of which kind produced it).

# G20: cadence for `response.heartbeat` SSE events emitted while
# the parent loop is blocked on the async-tool drain. 15 s keeps
# proxies that close idle connections at 30 s safely under their
# threshold without flooding the channel with pings.

# How often the blocking drain wakes to poll the conversation
# store for steering messages. Decoupled from
# ``_HEARTBEAT_INTERVAL_S`` because they answer different
# questions: heartbeat is for SSE-proxy keepalive (ceiling on
# how long a connection can stay idle), steering poll is for
# user-perceived latency (floor on how quickly the agent
# reacts to a mid-flight "hello"). 1 s is fast enough that
# steering feels instant while keeping conv_store load to
# 1 QPS per blocked workflow.

# Generic type variable used by ``_to_thread`` (pure helper).

# Hard upper bound on LLM turns per execution. Prevents runaway
# loops. See designs/AGENTLOOP.md "Not Yet" for making this
# configurable.

# SSE event types emitted for reasoning content (set by the
# streaming accumulator and consumed by the terminal frontend).

# Executor storage layout — each (conversation, agent) gets a
# stable subdir under ``_EXECUTOR_STORAGE_BASE`` that persists
# across tasks. The artifact-store key prefix mirrors the disk
# layout so snapshots round-trip cleanly.

# Client-side tool result polling — used by
# ``_build_await_tool_output`` while waiting for a PATCH'd
# function_call_output to arrive. 50 ms keeps interactive REPL
# turns snappy (a 5-tool turn loses at most 250 ms to polling
# instead of 2.5 s) while keeping the DB query cost negligible
# (~20 short SELECTs/sec while waiting). See
# designs/REPL_STREAMING_THROUGHPUT.md bottleneck #2.
# Translates Omnigent spec executor types (underscore) to harness subprocess names
# (hyphen), e.g. ``"claude_sdk"`` → ``"claude-sdk"`` used by ``_HARNESS_MODULES``.


AgentHarnessType = Literal["claude-sdk", "pi"]


@dataclass(frozen=True)
class _GatewayEnvKeys:
    """The ``HARNESS_*`` env vars one gateway-style harness reads.

    :param model_key: Harness model env var.
    :param base_url_key: Harness gateway base URL env var.
    :param host_key: Harness gateway host env var.
    :param auth_key: Harness gateway auth command env var.
    """

    model_key: str
    base_url_key: str
    host_key: str
    auth_key: str


_GATEWAY_ENV_KEYS: dict[AgentHarnessType, _GatewayEnvKeys] = {
    "claude-sdk": _GatewayEnvKeys(
        model_key="HARNESS_CLAUDE_SDK_MODEL",
        base_url_key="HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL",
        host_key="HARNESS_CLAUDE_SDK_GATEWAY_HOST",
        auth_key="HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND",
    ),
}


# Lazy singleton — created on first LLM call so import doesn't
# fail when provider API keys are not yet set.
_llm_client: LLMClient | None = None


def _get_llm_client() -> LLMClient:
    """Return the shared LLM client, creating it on first use."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def _get_runner_client_for_compaction(
    conversation_id: str | None,
) -> Any | None:
    """
    Return the httpx client for the runner handling *conversation_id*.

    Used by compaction call sites so Layer 2 summarization is routed
    through the runner's own credentials instead of the Omnigent server's.

    Returns ``None`` when:
    - ``conversation_id`` is ``None`` (in-process / no-server mode).
    - No ``RunnerRouter`` is wired up (unit tests, direct execution).
    - The conversation is not yet pinned to a runner.

    Propagates :class:`OmnigentError` when the pinned runner is
    offline so the caller can surface the error cleanly.

    :param conversation_id: Conversation id, e.g.
        ``"conv_0123456789abcdef"``, or ``None``.
    :returns: An :class:`httpx.AsyncClient` tunneled to the runner,
        or ``None`` when no runner is available.
    """
    if conversation_id is None:
        return None
    from omnigent.runtime import get_runner_router

    router = get_runner_router()
    if router is None:
        return None
    routed = router.client_for_existing_conversation(conversation_id)
    return routed.client if routed else None


# Maps single-family harnesses to the generic-provider family they consume.
# (``pi`` is handled separately — it consumes both families.) The keys are
# the canonical harness names used by the Chunk-1a provider-config layer
# (``omnigent/onboarding/provider_config.py`` ``_HARNESS_FAMILY``).
_PROVIDER_HARNESS_FAMILY: dict[AgentHarnessType, str] = {
    "claude-sdk": ANTHROPIC_FAMILY,
}

# Maps harnesses that gate the vendor-neutral gateway transport on a
# ``HARNESS_*_GATEWAY`` truthy flag to that env var name. The flag enables
# the executor's gateway path (base URL + token command + model) regardless
# of which provider fed it.
_HARNESS_GATEWAY_FLAG: dict[AgentHarnessType, str] = {
    "claude-sdk": "HARNESS_CLAUDE_SDK_GATEWAY",
    "pi": "HARNESS_PI_GATEWAY",
}

# Maps a generic-provider family to the key pi uses in its
# ``HARNESS_PI_GATEWAY_BASE_URLS`` JSON object (pi's own family naming).
_PI_FAMILY_KEY: dict[str, str] = {
    ANTHROPIC_FAMILY: "claude",
    OPENAI_FAMILY: "openai",
}


def _provider_auth_command(family: FamilyConfig) -> str:
    """Return a bearer-token shell command for *family*, failing loud if absent.

    Mirrors the executors' transport contract: the executors' gateway path
    invokes a shell command that prints the bearer token. A static
    ``api_key`` (already resolved to plaintext by
    :meth:`ProviderEntry.family`) becomes ``printf %s <shlex-quoted-key>``;
    a user-supplied dynamic ``auth_command`` passes through verbatim.

    :param family: The resolved provider family (``base_url`` + secret
        expanded by :meth:`ProviderEntry.family`).
    :returns: A shell command that prints the bearer token to stdout, e.g.
        ``"printf %s sk-or-abc"`` or the literal ``auth_command``.
    :raises OmnigentError: If the family carries neither a static
        ``api_key`` nor an ``auth_command`` (should not happen post-parse).
    """
    if family.api_key is not None:
        # printf %s avoids the trailing newline ``echo`` would add and is
        # shell-safe for keys with special characters via shlex.quote.
        return f"printf %s {shlex.quote(family.api_key)}"
    if family.auth_command is not None:
        return family.auth_command
    raise OmnigentError(
        "provider family has no credential (neither 'api_key' nor 'auth_command') "
        "to build a bearer-token command from.",
        code=ErrorCode.INVALID_INPUT,
    )


def _origin_of(base_url: str) -> str:
    """Return the scheme://host[:port] origin of *base_url*.

    The gateway executors expect a ``HARNESS_*_GATEWAY_HOST`` workspace
    origin separate from the full base URL (which carries the API path).

    :param base_url: The endpoint base URL, e.g.
        ``"https://openrouter.ai/api/v1"`` or ``"http://localhost:4000/v1"``.
    :returns: The origin, e.g. ``"https://openrouter.ai"`` or
        ``"http://localhost:4000"``.
    """
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def configure_agent_harness_with_provider(
    env: dict[str, str],
    entry: ProviderEntry,
    *,
    harness_type: AgentHarnessType,
) -> None:
    """Inject per-harness model, URL, and auth from a generic provider.

    Takes a resolved :class:`ProviderEntry` (from the ``providers:`` block
    of ``~/.omnigent/config.yaml``) and emits the vendor-neutral
    ``HARNESS_*_GATEWAY_*`` env vars (base URL, host, a bearer-token command,
    model default), so the executors' gateway path handles a LiteLLM /
    OpenRouter / local endpoint. Dispatch is on :attr:`ProviderEntry.kind`:

    - ``key`` / ``gateway`` / ``local`` — resolve the harness's family and
      emit the ``HARNESS_*_GATEWAY_*`` env vars (see
      :func:`_apply_provider_family`).
    - ``subscription`` — the CLI harness carries its own login; no gateway
      vars.
    - ``bedrock`` — rejected (raises): AWS Bedrock mode is wired only into
      the native ``omnigent claude`` launch, not the in-process / gateway
      harnesses.

    :param env: Mutable spawn-env dict, modified in place.
    :param entry: The resolved provider entry to apply.
    :param harness_type: Canonical harness type, e.g. ``"claude-sdk"``.
    :raises OmnigentError: If an inline-family provider lacks the family
        the harness requires, or no model can be resolved for it.
    """
    if entry.kind == BEDROCK_KIND:
        # The in-process / gateway harnesses have no Bedrock path, so emitting
        # the generic ``HARNESS_*_GATEWAY_*`` vars would silently point the
        # harness at the Bedrock endpoint as if it spoke the Anthropic Messages
        # API and fail at request time. Fail loud instead.
        raise OmnigentError(
            f"provider {entry.name!r} (kind 'bedrock') is only supported by the "
            f"native 'omnigent claude' terminal, not the {harness_type!r} harness. "
            "For agents / 'omnigent run', use a 'gateway' provider "
            "(OpenAI/Anthropic-compatible endpoint), or a 'key' provider.",
            code=ErrorCode.INVALID_INPUT,
        )
    if entry.kind == SUBSCRIPTION_KIND:
        # A logged-in CLI carries its own auth; the harness reads its own
        # login. Emitting inline-family gateway vars here would point the
        # harness at a non-existent endpoint.
        return

    # Inline-family kinds: key / gateway / local.
    if harness_type == "pi":
        _apply_provider_to_pi(env, entry)
        return
    family_name = _PROVIDER_HARNESS_FAMILY[harness_type]
    family = entry.family(family_name)
    if family is None:
        raise OmnigentError(
            f"provider {entry.name!r} has no {family_name!r} family, required by the "
            f"{harness_type!r} harness. Add a '{family_name}:' block to that provider in "
            f"~/.omnigent/config.yaml.",
            code=ErrorCode.INVALID_INPUT,
        )
    _apply_provider_family(env, harness_type, family)


# Maps an omnigent provider family to the bundled catalog provider name
# whose default model serves it. The two happen to share names today
# (``anthropic`` family ⇄ ``anthropic`` catalog, ``openai`` family ⇄
# ``openai`` catalog), but routing through this map keeps the family→catalog
# coupling explicit and one place to change if a family ever fans out to a
# differently-named catalog (e.g. an openai-compatible vendor).
_FAMILY_CATALOG_TARGET: dict[str, tuple[str, str]] = {
    ANTHROPIC_FAMILY: ("anthropic", "claude"),
    OPENAI_FAMILY: ("openai", "openai"),
}


def _resolve_catalog_default_model(
    provider_name: str,
    family: str,
    *,
    context: str,
) -> str:
    """Resolve one live catalog default or raise an actionable input error."""
    try:
        return resolve_catalog_model(provider_name, family=family).model_id
    except ModelResolutionError as exc:
        raise OmnigentError(
            f"No default model resolved for {context}: the {provider_name!r} model "
            f"catalog has no compatible {family!r} entry. Set 'executor.model' in "
            "the agent YAML or a provider 'models.default', or retry when catalog "
            "discovery is available.",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def _catalog_default_model(family_name: str) -> str:
    """Return the bundled catalog's default model for a provider family.

    Used as the model-resolution fallback for a ``key`` / ``gateway`` /
    ``local`` provider on a KNOWN family (anthropic / openai) when neither
    the spec nor the provider's ``models.default`` names a model: rather than
    fail loud, resolve a sensible vendor model from the catalog. The neutral
    gateway path never falls back to a gateway-specific model. This is a real,
    designed default — see
    :func:`omnigent.onboarding.providers.default_chat_model` for the rule
    (newest general-purpose chat model for that vendor) — not an invented
    one masking missing data: it only applies on a family the catalog knows.

    :param family_name: The omnigent family, ``"anthropic"`` or
        ``"openai"``.
    :returns: The live catalog's preferred model id.
    :raises OmnigentError: If the family is unknown or discovery has no
        compatible model.
    """
    target = _FAMILY_CATALOG_TARGET.get(family_name)
    if target is None:
        raise OmnigentError(
            f"No model catalog is configured for provider family {family_name!r}.",
            code=ErrorCode.INVALID_INPUT,
        )
    provider_name, catalog_family = target
    return _resolve_catalog_default_model(
        provider_name,
        catalog_family,
        context=f"provider family {family_name!r}",
    )


def _apply_provider_family(
    env: dict[str, str],
    harness_type: AgentHarnessType,
    family: FamilyConfig,
) -> None:
    """Apply a provider family to the claude-sdk harness's gateway path.

    Emits the vendor-neutral ``HARNESS_*_GATEWAY_*`` env vars the
    executor's gateway path reads. The model default is only applied when the spec did
    not already set the model env var.

    :param env: Mutable spawn-env dict, modified in place.
    :param harness_type: ``"claude-sdk"``.
    :param family: The resolved provider family for this harness.
    :raises OmnigentError: If no model is resolvable (neither the spec nor
        the family declares one).
    """

    cfg = _GATEWAY_ENV_KEYS[harness_type]
    env[_HARNESS_GATEWAY_FLAG[harness_type]] = "true"
    env[cfg.base_url_key] = family.base_url
    env[cfg.host_key] = _origin_of(family.base_url)
    env[cfg.auth_key] = _provider_auth_command(family)
    # Model precedence: spec model (already in env via _resolve_spec_model) >
    # provider ``models.default`` > catalog family default > fail loud.
    if cfg.model_key not in env and family.default_model:
        env[cfg.model_key] = family.default_model
    if cfg.model_key not in env:
        env[cfg.model_key] = _catalog_default_model(_PROVIDER_HARNESS_FAMILY[harness_type])


def _optional_provider_family(
    entry: ProviderEntry, family_name: str
) -> tuple[FamilyConfig | None, OmnigentError | None]:
    """Attempt to resolve a provider family, returning success or failure info.

    For the ``pi`` harness, which carries a single credential but probes
    both families: a family whose ``$VAR`` is unresolved is treated as
    unavailable rather than fatal, so e.g. a user who only exported
    ``ANTHROPIC_API_KEY`` can still run pi on the anthropic family.

    All credential resolution failures (unresolved ``env:`` var, missing
    keychain secret) raise :class:`OmnigentError` at family-access time
    (structural validation already happened at parse time). They are caught
    here so the other family can be tried; the error is returned to the
    caller so it can be surfaced when no family succeeds.

    Returns a two-tuple ``(family, error)`` so the caller can include the
    original resolution error in its failure message, naming the missing
    variable instead of emitting a generic "no family resolves" message.

    :param entry: The resolved provider entry.
    :param family_name: Family key, e.g. ``"openai"`` or ``"anthropic"``.
    :returns: ``(FamilyConfig, None)`` when the family resolved, or
        ``(None, OmnigentError)`` when resolution failed, or ``(None, None)``
        when the family is simply not configured.
    """
    try:
        family = entry.family(family_name)
        return family, None
    except OmnigentError as exc:
        return None, exc


def _apply_provider_to_pi(env: dict[str, str], entry: ProviderEntry) -> None:
    """Apply a provider to the pi harness, which consumes both families.

    pi reads ``HARNESS_PI_GATEWAY_BASE_URLS`` (a JSON object keyed by pi's
    own family names) and a single auth command. When both families are
    configured with different credentials pi can only carry one — it uses the
    ``anthropic`` family's auth when present, else the ``openai`` family's.
    For a single-key gateway (e.g. a LiteLLM proxy) this is exact.

    A family whose credential env var is unset is skipped (not fatal) so a
    user who exported only one vendor's key can still run pi on that family.
    If neither family resolves, this fails loud, including the original
    credential resolution error(s) so the user knows which env var to set.

    :param env: Mutable spawn-env dict, modified in place.
    :param entry: The resolved provider entry (at least one inline family).
    :raises OmnigentError: If no configured family's credentials resolve,
        or no model can be resolved for the chosen family.
    """
    anthropic, anthropic_err = _optional_provider_family(entry, ANTHROPIC_FAMILY)
    openai, openai_err = _optional_provider_family(entry, OPENAI_FAMILY)
    base_urls: dict[str, str] = {}
    if anthropic is not None:
        base_urls[_PI_FAMILY_KEY[ANTHROPIC_FAMILY]] = anthropic.base_url
    if openai is not None:
        base_urls[_PI_FAMILY_KEY[OPENAI_FAMILY]] = openai.base_url
    if not base_urls:
        # At least one family was configured (the provider passed parse-time
        # validation) but its credential could not be resolved. Surface the
        # original error(s) so the user knows which env var to set, rather
        # than a generic "set the api_key env var" message that omits the name.
        cred_errors = [str(err) for err in (anthropic_err, openai_err) if err is not None]
        detail = (
            f" ({'; '.join(cred_errors)})"
            if cred_errors
            else " — set the api_key env var for its 'anthropic' or 'openai' family in your shell"
        )
        raise OmnigentError(
            f"pi harness: provider {entry.name!r} configures no family whose "
            f"credentials resolve{detail}, then retry.",
            code=ErrorCode.INVALID_INPUT,
        )
    # pi carries a single credential: anthropic's when present, else openai's.
    # The model fallback must match the family that supplied that credential.
    auth_source: FamilyConfig | None
    if anthropic is not None:
        auth_source = anthropic
        auth_family = ANTHROPIC_FAMILY
    else:
        auth_source = openai
        auth_family = OPENAI_FAMILY
    assert auth_source is not None  # base_urls non-empty ⇒ one family resolved
    env[_HARNESS_GATEWAY_FLAG["pi"]] = "true"
    env["HARNESS_PI_GATEWAY_BASE_URLS"] = json.dumps(base_urls, sort_keys=True)
    # Configured model limits, keyed like the base URLs; pi otherwise assumes 128k / 16k.
    model_limits: dict[str, dict[str, int]] = {}
    for family_name, family in ((ANTHROPIC_FAMILY, anthropic), (OPENAI_FAMILY, openai)):
        if family is None:
            continue
        limits = {
            key: value
            for key, value in (
                ("context_window", family.context_window),
                ("max_output_tokens", family.max_output_tokens),
            )
            if value is not None
        }
        if limits:
            model_limits[_PI_FAMILY_KEY[family_name]] = limits
    if model_limits:
        env["HARNESS_PI_GATEWAY_MODEL_LIMITS"] = json.dumps(model_limits, sort_keys=True)
    if openai is not None and openai.wire_api is not None:
        env["HARNESS_PI_GATEWAY_OPENAI_WIRE_API"] = openai.wire_api
    env["HARNESS_PI_GATEWAY_HOST"] = _origin_of(next(iter(base_urls.values())))
    env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] = _provider_auth_command(auth_source)
    # Model precedence: spec model > provider ``models.default`` > catalog
    # family default (of the auth-source family) > fail loud.
    if "HARNESS_PI_MODEL" not in env and auth_source.default_model:
        env["HARNESS_PI_MODEL"] = auth_source.default_model
    if "HARNESS_PI_MODEL" not in env:
        env["HARNESS_PI_MODEL"] = _catalog_default_model(auth_family)


def _resolve_provider_for_build(
    spec: AgentSpec,
    *,
    harness_type: AgentHarnessType,
    for_launch: bool = False,
    actual_harness: str | None = None,
) -> ProviderEntry | None:
    """Resolve the provider that should route *harness_type*, if any.

    The single credential resolver, shared by the spawn-env builders (with
    *for_launch*) and the readout / cost / native paths (without). Precedence,
    most explicit first:

    1. ``spec.executor.auth`` is a :class:`ProviderAuth` → that named provider
       (fails loud when undeclared).
    2. An :class:`ApiKeyAuth` (spec or global) → ``None`` (the claude-sdk
       builder threads the key itself).
    3. The per-family global default (``providers: … default: true``), then an
       ambient-detected default.
    4. (``for_launch`` only) the first credential that can serve the family even
       though it is not marked default — so a launch credentials the head
       rather than failing with "Invalid API key". Off for the readout / cost
       paths so they never show a provider the user did not choose.

    :param spec: The agent spec.
    :param harness_type: Canonical workflow harness type, e.g. ``"claude-sdk"``.
    :param for_launch: ``True`` for the spawn-env builders (permissive: fall
        back to the first available credential). ``False`` (readout / cost /
        native) keeps strict, config-only resolution with no fallback.
    :param actual_harness: Preserve a native harness identity when its transport
        reuses an SDK provider adapter.
    :returns: The :class:`ProviderEntry` to route through, or ``None``.
    :raises OmnigentError: If a named :class:`ProviderAuth` references a
        provider absent from the ``providers:`` block.
    """
    from omnigent.inference_config import load_runtime_inference_config, resolve_bound_provider

    explicit_config = load_runtime_inference_config(load_config())
    identity = actual_harness or str(spec.executor.config.get("harness") or harness_type)
    bound = resolve_bound_provider(explicit_config, identity, spec.executor.auth)
    if bound is not None:
        return bound
    harness = harness_type
    auth = spec.executor.auth
    if isinstance(auth, ProviderAuth):
        # A named provider is resolved against the explicit config merged with
        # ambient detections, so a spec may name a detected provider too.
        providers = load_providers(effective_config_with_detected(explicit_config))
        entry = providers.get(auth.name)
        if entry is None:
            raise OmnigentError(
                f"executor.auth references provider {auth.name!r}, but no such provider is "
                "configured under 'providers:' in ~/.omnigent/config.yaml.",
                code=ErrorCode.INVALID_INPUT,
            )
        return entry
    if auth is not None:
        # ApiKeyAuth — threaded by the claude-sdk builder.
        return None

    # No spec auth. Most explicit wins, ambient last, then a launch-only fallback.
    explicit_default = default_provider_for_harness(explicit_config, harness)
    if explicit_default is not None:
        return explicit_default
    global_auth = _load_global_auth()
    if global_auth is not None:
        # Global ApiKeyAuth — threaded by the builder's global-auth branch.
        return None
    effective = effective_config_with_detected(explicit_config)
    ambient_default = default_provider_for_harness(effective, harness)
    if ambient_default is not None:
        return ambient_default
    # Launch-only last resort: no default anywhere, but a credential that serves
    # this family is configured (one the user added but never set as the
    # default). The runner is the one chokepoint every head
    # (CLI, web UI, or a remote host) funnels through, so this credentials the
    # head on every surface, for any agent. Resolved per spawn — nothing is
    # persisted; the startup creds line names the same provider via
    # :func:`first_available_provider`, so the readout cannot disagree.
    if for_launch:
        family = harness_family(harness)
        if family is not None:
            return first_available_provider(effective, family)
    return None


def _resolve_spec_model(spec: AgentSpec) -> str | None:
    """
    Return the model identifier from the spec's executor block.

    :param spec: The agent spec.
    :returns: The model identifier, or ``None`` when the spec
        declares no model.
    """
    return spec.executor.model


def _resolve_bound_launch_model(spec: AgentSpec, harness: str) -> str | None:
    """Validate the selected model against this session's inference binding."""
    from omnigent.inference_config import load_runtime_inference_config, resolve_bound_model

    identity = str(spec.executor.config.get("harness") or harness)
    return resolve_bound_model(
        load_runtime_inference_config(load_config()), identity, _resolve_spec_model(spec)
    )


def _add_claude_sdk_skills_env(
    env: dict[str, str],
    spec: AgentSpec,
    workdir: Path | None,
) -> None:
    """
    Populate the skills-related ``HARNESS_CLAUDE_SDK_*`` env vars.

    Threads ``spec.skills_filter`` (always — the harness wrap
    falls back to ``"all"`` on a missing var, which would
    silently override an explicit ``skills: none``), ``spec.name``,
    and the bundle's extracted on-disk path so the harness wrap
    can wire ``ClaudeAgentOptions.plugins`` for agent-bundled
    skills.

    :param env: Dict mutated in-place with the new keys.
    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path (extracted by the
        agent cache). ``None`` skips the plugin-dir wiring.
    """
    # ``skills_filter`` is JSON-encoded because the value can be
    # ``"all"`` / ``"none"`` / a list of skill names. Always set:
    # the harness wrap's ``_resolve_skills_filter`` falls back to
    # ``"all"`` on a missing var, which would silently override
    # an explicit ``skills: none`` from the spec. (The original
    # regression that motivated this whole bridge.)
    env["HARNESS_CLAUDE_SDK_SKILLS_FILTER"] = json.dumps(spec.skills_filter)
    if spec.name:
        env["HARNESS_CLAUDE_SDK_AGENT_NAME"] = spec.name
    if str(spec.executor.config.get("native_skills")).lower() == "false":
        # Omnigent owns this agent's skills (``load_skill``): no plugin, no Skill tool. A
        # Sub-agent Type's config reaches here string-coerced ("False"), so both forms count.
        env["HARNESS_CLAUDE_SDK_NATIVE_SKILLS"] = "0"
    elif workdir is not None:
        env["HARNESS_CLAUDE_SDK_BUNDLE_DIR"] = str(workdir)


def _build_claude_sdk_spawn_env(
    spec: AgentSpec,
    *,
    cwd: Path | None = None,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the env-var dict the claude-sdk harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_CLAUDE_SDK_*`` env
    vars defined in ``omnigent/harnesses/claude_sdk_harness.py``.
    Per the v1 spec-config-flow design (see §Step 5b in the
    design doc), per-spawn env overrides are how Omnigent threads
    per-spec config into the subprocess without polluting
    ``os.environ``.

    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path (extracted by the
        agent cache). Threaded through as
        ``HARNESS_CLAUDE_SDK_BUNDLE_DIR`` so the harness wrap can
        wire the SDK ``--plugin-dir`` for agent-bundled skills.
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`.
    """
    env: dict[str, str] = {}
    model = _resolve_bound_launch_model(spec, "claude-sdk")
    if model is not None:
        from omnigent.inference_config import binding_for_harness, load_runtime_inference_config

        # Specs may pin the provider-routed spelling ("anthropic/<name>") so
        # generic clients route correctly, but the claude CLI rejects
        # vendor prefixes. A bound gateway owns its literal model namespace.
        env["HARNESS_CLAUDE_SDK_MODEL"] = (
            model
            if binding_for_harness(load_runtime_inference_config(), "claude-sdk") is not None
            else model.removeprefix("anthropic/")
        )
    # Session workspace (the selected working folder), not the bundle workdir.
    # Without this the SDK subprocess inherits the runner's launch cwd — see
    # ``HARNESS_CLAUDE_SDK_CWD`` in ``omnigent/harnesses/claude_sdk_harness.py``.
    if cwd is not None:
        env["HARNESS_CLAUDE_SDK_CWD"] = str(cwd)

    # ── Auth resolution ────────────────────────────────────────────────
    # Priority (highest first):
    # 0. Generic provider — spec.executor.auth: {type: provider, name: X},
    #    OR (no spec auth) the per-family global default from the
    #    ``providers:`` config block (:func:`_resolve_provider_for_build`).
    #    Routes a LiteLLM / OpenRouter / local provider.
    # 1. spec.executor.auth — explicit typed auth in the agent YAML.
    # 2. Global config ~/.omnigent/config.yaml auth: — only when spec has
    #    no auth at all.
    provider = _resolve_provider_for_build(spec, harness_type="claude-sdk", for_launch=True)
    if provider is not None:
        configure_agent_harness_with_provider(env, provider, harness_type="claude-sdk")
    else:
        # No provider resolved → the only remaining credential is an ApiKeyAuth
        # (spec ``executor.auth`` or the global ``auth:`` block). The executor
        # strips ANTHROPIC_API_KEY to force subscription auth inside Claude
        # Code, so the key is threaded via the CLI's apiKeyHelper (a shell
        # command the CLI invokes; shlex.quote keeps it shell-safe).
        auth_from_spec = spec.executor.auth
        if auth_from_spec is None:
            auth_from_spec = _load_global_auth()
        if isinstance(auth_from_spec, ApiKeyAuth) and auth_from_spec.api_key:
            _key_cmd = f"printf %s {shlex.quote(auth_from_spec.api_key)}"
            env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"] = _key_cmd
            if auth_from_spec.base_url:
                env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] = auth_from_spec.base_url
                env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] = _key_cmd
        # Enable the gateway for an ApiKeyAuth ``base_url`` (a custom endpoint);
        # without the flag the executor ignores the base URL and falls through
        # to api.anthropic.com.
        if isinstance(auth_from_spec, ApiKeyAuth) and auth_from_spec.base_url:
            env["HARNESS_CLAUDE_SDK_GATEWAY"] = "true"
    _add_claude_sdk_skills_env(env, spec, workdir)
    # OS env: enabling this in the inner ClaudeSDKExecutor is
    # what gates the SDK-native ``Bash/Read/Edit/Write/Glob/Grep``
    # tools. The legacy non-AP path enables them by default
    # for omnigent-style specs (the inner CLI auto-creates a
    # ``caller_process`` os_env when ``--os`` is set, and the
    # SDK's bundled CLI exposes the natives unconditionally in
    # some configurations); routing through Omnigent mode without a
    # similar default leaves Omnigent mode users staring at a
    # tool list that's ~80% smaller. Forward the spec's
    # OSEnvSpec verbatim when present; default to
    # ``caller_process + sandbox=none`` otherwise so the parity
    # holds.
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_CLAUDE_SDK_OS_ENV"] = os_env_payload
    # Phase 1f: thread the spec's RetryPolicy through to the
    # claude-sdk subprocess so the inner ``ClaudeSDKExecutor``
    # picks up custom retry budgets (``ANTHROPIC_MAX_RETRIES``,
    # ``ANTHROPIC_REQUEST_TIMEOUT_SECONDS``) instead of falling
    # back to the executor's hard-coded ``RetryPolicy()`` default.
    # Omitted when the policy matches defaults — see
    # :func:`_serialize_retry_policy`.
    retry_payload = _serialize_retry_policy(_resolve_retry_policy(spec))
    if retry_payload is not None:
        env["HARNESS_CLAUDE_SDK_RETRY_POLICY"] = retry_payload
    # Permission mode: controls whether Claude asks for approval before
    # calling native tools. When set to anything other than the default
    # ``"bypassPermissions"``, the SDK's ``can_use_tool`` callback is
    # active and approval requests surface via Omnigent elicitation. Read from
    # Omitted when not set — harness falls back to ``"bypassPermissions"``.
    permission_mode = spec.executor.config.get("permission_mode")
    if permission_mode is not None:
        env["HARNESS_CLAUDE_SDK_PERMISSION_MODE"] = str(permission_mode)
    return env


def _apply_harness_path_override(
    env: dict[str, str],
    harness: str,
) -> None:
    """Thread a config ``harness.<canonical>.command`` into ``OMNIGENT_<NAME>_PATH``.

    The harness wraps read ``OMNIGENT_<NAME>_PATH`` to locate their vendor
    CLI (the headless CLI-subprocess family historically read ``HARNESS_*_PATH``;
    both are honored, ``OMNIGENT_*`` canonical). A user can set that path via
    config (``harness.pi.command: /usr/local/bin/pi``); this threads it
    into the spawn env when the ambient env var isn't already set (ambient
    wins, per the shared ``env > config > default`` precedence). A no-op when
    config has no ``command`` for *harness* or the ambient env var is set.

    :param env: The spawn-env dict being built (mutated in place).
    :param harness: A harness id (canonical or alias), e.g. ``"pi"``.
    """
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.harness_startup_config import (
        _harness_path_env_var,
        config_harness_path_override,
    )

    path = config_harness_path_override(harness, load_config())
    if path is not None:
        env[_harness_path_env_var(canonicalize_harness(harness) or harness)] = path


def _build_pi_spawn_env(
    spec: AgentSpec,
    *,
    cwd: Path | None = None,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the env-var dict the pi harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_PI_*`` env vars
    defined in ``omnigent/harnesses/pi_harness.py``. Mirrors
    :func:`_build_claude_sdk_spawn_env` — same per-spawn env-var
    pattern from §Step 5a.

    :param spec: The agent spec.
    :param cwd: Runtime working directory for the Pi CLI. This is the
        session workspace, not the agent bundle workdir.
    :param workdir: The bundle's on-disk path (extracted by the
        agent cache). Threaded through as ``HARNESS_PI_BUNDLE_DIR``
        so the harness wrap's executor can source bundled skills
        from ``<bundle>/skills/<dir>/``.
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`.
    """
    env: dict[str, str] = {}
    model = _resolve_bound_launch_model(spec, "pi")
    from omnigent.inference_config import binding_for_harness, load_runtime_inference_config

    if binding_for_harness(load_runtime_inference_config(), "pi") is not None:
        env["HARNESS_PI_PRESERVE_MODEL_IDS"] = "true"
    if model is not None:
        env["HARNESS_PI_MODEL"] = model

    # Generic-provider branch: a ProviderAuth on the spec, or — when the spec
    # declares no auth — the per-family global default. pi consumes both
    # families (see :func:`_apply_provider_to_pi`).
    provider = _resolve_provider_for_build(spec, harness_type="pi", for_launch=True)
    if provider is not None:
        configure_agent_harness_with_provider(env, provider, harness_type="pi")
        if "HARNESS_PI_PRESERVE_MODEL_IDS" in env and provider.family(OPENAI_FAMILY) is not None:
            env.setdefault("HARNESS_PI_GATEWAY_OPENAI_WIRE_API", RESPONSES_WIRE_API)
    # Skills bridge — same shape as the claude-sdk variant.
    # Always set so the harness wrap doesn't fall back to ``"all"``
    # and override an explicit ``skills: none`` from the spec.
    env["HARNESS_PI_SKILLS_FILTER"] = json.dumps(spec.skills_filter)
    env["HARNESS_PI_CONTEXT_FILES"] = json.dumps(spec.executor.config.get("context_files", True))
    env["HARNESS_PI_SYSTEM_PROMPT_MODE"] = spec.executor.config.get("system_prompt_mode", "append")
    if spec.name:
        env["HARNESS_PI_AGENT_NAME"] = spec.name
    if cwd is not None:
        env["HARNESS_PI_CWD"] = str(cwd)
    if workdir is not None:
        env["HARNESS_PI_BUNDLE_DIR"] = str(workdir)
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_PI_OS_ENV"] = os_env_payload
    _apply_harness_path_override(env, "pi")
    return env


def _load_global_auth() -> ApiKeyAuth | None:
    """
    Load the ``auth:`` block from ``~/.omnigent/config.yaml``.

    Reads the user-level global config file (respecting
    ``$OMNIGENT_CONFIG_HOME`` for test isolation) and parses the
    optional ``auth:`` mapping into a typed auth dataclass.  Returns
    ``None`` when the file does not exist, the ``auth:`` key is absent,
    or the block is not a recognized shape.

    This provides a user-level auth default: agents that do not declare
    ``executor.auth`` in their own spec inherit credentials from here,
    so the user only configures auth once during ``omnigent setup``
    rather than in every agent YAML.

    :returns: A :class:`ApiKeyAuth`, or ``None`` when the global config
        has no ``auth:`` block or the file is missing.
    """
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    path = (
        Path(config_home) / "config.yaml"
        if config_home
        else Path.home() / ".omnigent" / "config.yaml"
    )
    if not path.exists():
        return None
    try:
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
    except Exception:
        return None
    raw_auth = raw.get("auth")
    if not isinstance(raw_auth, dict):
        return None
    auth_type = str(raw_auth.get("type", ""))
    if auth_type == "api_key":
        api_key = str(raw_auth.get("api_key") or "")
        if not api_key:
            return None
        # Expand $VAR references (the config file may store the literal
        # env-var reference; expand at use-time so the secret never
        # needs to live in the YAML file itself).
        api_key = expand_envvars_with_omnigent_prefix(api_key)
        check_unresolved_env_vars("auth.api_key", api_key)
        raw_base_url = raw_auth.get("base_url")
        base_url: str | None = None
        if raw_base_url:
            base_url = expand_envvars_with_omnigent_prefix(str(raw_base_url))
            check_unresolved_env_vars("auth.base_url", base_url)
        return ApiKeyAuth(api_key=api_key, base_url=base_url)
    return None


def _serialize_os_env(value: OSEnvSpec | None) -> str | None:
    """
    Encode an :class:`OSEnvSpec` for the wrap's env-var input.

    JSON-encodes :func:`dataclasses.asdict` of the OSEnvSpec so
    the wrap can :func:`json.loads` it back on the harness side
    (per the per-spawn env-var pattern from §Step 5a). When
    *value* is ``None`` (no os_env declared on the spec), this
    returns ``None`` and ``_build_claude_sdk_spawn_env`` omits
    the env var entirely — the wrap then falls back to its
    enable-natives-by-default rule.

    :param value: ``spec.os_env`` — an :class:`OSEnvSpec`
        instance or ``None``.
    :returns: JSON string encoding the OSEnvSpec, or ``None``
        when *value* is ``None``.
    """
    import dataclasses

    if value is None:
        return None
    return json.dumps(dataclasses.asdict(value))


def _serialize_retry_policy(value: RetryPolicy | None) -> str | None:
    """
    Encode a :class:`RetryPolicy` for the wrap's env-var input.

    Phase 1f of ``designs/RETRY_ACROSS_HARNESSES.md``: the spec's
    ``LLMConfig.retry`` must take effect inside the CLI-harness
    subprocess. Serializing the whole policy as one JSON env var
    keeps the wire format compact (~150 bytes) and lets the
    harness round-trip via :func:`json.loads` →
    ``RetryPolicy(**dict)``. JSON over a flat fan-out of
    ``HARNESS_*_RETRY_MAX_RETRIES`` / ``..._BACKOFF_BASE_S`` /
    etc. because (a) ``RetryPolicy`` has 6 fields including a
    tuple, and a flat fan-out would multiply boilerplate at
    every wrap; (b) future field additions stay
    backwards-compatible — older wraps just ignore unknown
    keys via the ``__init__`` filter below.

    Returns ``None`` when the policy matches
    ``RetryPolicy()`` defaults so the env var is omitted —
    saves the harness wrap an unnecessary parse step on the
    common path.

    :param value: ``llm_config.retry`` — a :class:`RetryPolicy`
        or ``None``. ``None`` and ``RetryPolicy()`` (defaults)
        both produce ``None`` so the wrap falls back to the
        same baked-in default.
    :returns: JSON string encoding the policy's fields, or
        ``None`` when *value* is ``None`` or matches the
        defaults.
    """
    import dataclasses
    import json

    if value is None or value == RetryPolicy():
        return None
    payload = dataclasses.asdict(value)
    # ``retryable_status_codes`` is a tuple in the dataclass;
    # ``asdict`` converts it to a list. JSON has no tuple type —
    # the harness side reconstructs the tuple in
    # ``_deserialize_retry_policy``.
    return json.dumps(payload)


def _resolve_retry_policy(spec: AgentSpec) -> RetryPolicy | None:
    """
    Read the retry policy off a spec.

    Used by the per-harness ``_build_*_spawn_env`` builders to
    decide whether to thread a ``HARNESS_*_RETRY_POLICY`` env
    var through to the subprocess. Returns ``None`` when the
    spec has no ``llm`` block (e.g. a CLI-harness spec where
    the retry policy is implicit) so the harness wrap falls
    back to its baked-in :class:`RetryPolicy()` default.

    :param spec: The agent spec.
    :returns: ``spec.llm.retry`` if set; ``None`` otherwise.
    """
    if spec.llm is None:
        return None
    return spec.llm.retry


# ── Responses API helpers ─────────────────────────────────


def _apply_request_model_override(
    llm_config: LLMConfig,
    model_override: str | None,
) -> LLMConfig:
    """
    Substitute the per-request LLM model into the agent's LLM config.

    The override is also stashed in ``extra["model_override"]`` so the
    harness path can forward it to the subprocess — the harness body's
    ``model`` field carries the agent name, distinct from the LLM
    model. Returns a new :class:`LLMConfig`; original is not mutated.

    :param llm_config: The agent spec's LLM config (already merged
        with any per-request reasoning override).
    :param model_override: Per-request LLM model identifier, e.g.
        ``"claude-sonnet-4-6"``, or ``None`` to pass
        ``llm_config`` through unchanged.
    :returns: A (possibly new) :class:`LLMConfig`.
    """
    if model_override is None:
        return llm_config
    merged_extra = {**llm_config.extra, "model_override": model_override}
    return LLMConfig(
        model=model_override,
        extra=merged_extra,
        connection=llm_config.connection,
        request_timeout=llm_config.request_timeout,
        retry=llm_config.retry,
    )


# ── Checkpointed steps ───────────────────────────────────


# ── Executor @step wrapper ────────────────────────────────


# Maps executor reasoning event_type to SSE event type string.


# ── Executor turn → response dict bridge ──────────────────


def _prepare_messages(
    spec: AgentSpec,
    llm_config: LLMConfig,
    history: list[ConversationItem],
    instructions: str | None,
    tool_schemas: list[dict[str, Any]],
    compaction_state: _CompactionState,
    content_cache: dict[str, str] | None,
    *,
    conversation_id: str | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    """
    Build system instructions and Responses API input items.

    Resolves content references and counts system token budget.
    Extracted from ``_call_llm_maybe_compact`` for reuse by the
    executor path.

    :param spec: The parsed AgentSpec.
    :param llm_config: LLM configuration.
    :param history: Conversation history as persisted items.
    :param instructions: Optional per-request instructions.
    :param tool_schemas: OpenAI-format tool schemas.
    :param compaction_state: Per-execution compaction state.
    :param content_cache: Per-task content reference cache.
    :param conversation_id: Optional owning conversation/session id
        used to verify session-scoped file ownership.
    :returns: Tuple of (system_instructions, messages, sys_tokens).
    """
    sys_instructions = build_instructions(spec, instructions, tool_schemas)
    file_store = get_file_store()
    artifact_store = get_artifact_store()
    resolved = history
    if file_store is not None and artifact_store is not None:
        resolved = resolve_content_references(
            history,
            file_store,
            artifact_store,
            content_cache,
            session_id=conversation_id,
        )
    messages = history_to_input_items(resolved, preserve_framework_notices=True)
    sys_tokens = count_tokens(
        [{"role": "system", "content": sys_instructions}],
        compaction_state.model,
    )
    return sys_instructions, messages, sys_tokens


# ── Output helpers ────────────────────────────────────────


# ── Pagination helper ─────────────────────────────────────


@restart_on_stale_cursor
def fetch_all_items(
    conv_store: ConversationStore,
    conversation_id: str,
    after: str | None = None,
) -> list[ConversationItem]:
    """
    Fetch all conversation items starting after the given
    cursor, paginating through every page until ``has_more``
    is ``False``. An item cursor invalidated mid-walk restarts
    the walk (via :func:`restart_on_stale_cursor`) instead of
    silently dropping the remaining items.

    The restart only recovers a cursor this function derived
    itself. A caller-supplied ``after`` that is already gone
    would be re-issued unchanged by every attempt, so it
    raises — the caller decides what a missing anchor means
    for its own read.

    :param conv_store: The ConversationStore to query.
    :param conversation_id: The conversation to fetch items
        from, e.g. ``"conv_abc123"``.
    :param after: Cursor item ID to start after, or ``None``
        to fetch from the beginning.
    :returns: All items in chronological order after the
        cursor.
    :raises StaleCursorError: If ``after`` names an item that
        no longer exists.
    """
    all_items: list[ConversationItem] = []
    cursor = after
    while True:
        page = conv_store.list_items(conversation_id, after=cursor)
        all_items.extend(page.data)
        if not page.has_more:
            break
        # Advance cursor to the last item of this page
        cursor = page.last_id
    return all_items


# ── Extracted helpers ─────────────────────────────────────


def _strip_mcp_tool_prefix(name: str) -> str:
    """Strip ``mcp__<server>__`` prefix from *name*; preserve bare ``__``."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


# ─── Async tool dispatch ───────────────────────────────────


@dataclass
class _AsyncToolHandle:
    """
    Handle returned to the LLM when an async tool is dispatched.

    Replaces the inline tool result string from a sync invocation.
    The LLM gets back a structured task handle and waits for
    auto-delivery (D7/G12); explicit polling was dropped per
    design step 11.

    :param task_id: The newly created task's ID, e.g.
        ``"tsk_async_xyz"``. Identical to the
        the dispatching task's id (G56).
    :param tool_name: The dispatched tool's name, included in the
        handle so the LLM can correlate the handle to its own
        tool_calls field.
    :param status: Always ``"in_progress"`` at handle-creation
        time — terminal status arrives via the
        ``async_work_complete`` signal.
    :param message: A self-explanatory instruction for the LLM
        (G12). Names the task_id explicitly so the LLM can
        copy-paste it into ``sys_cancel_task`` if it wants
        to abort.
    """

    task_id: str
    tool_name: str
    status: str
    message: str

    def to_handle_json(self) -> str:
        """
        Serialize the handle as JSON for the tool-call return path.

        The runner contract returns strings, so the handle ships
        as a JSON-encoded dict. The LLM treats the result like
        any other tool output.

        :returns: JSON string with ``task_id``, ``tool_name``,
            ``status``, and ``message`` keys.
        """
        return json.dumps(
            {
                "task_id": self.task_id,
                "tool_name": self.tool_name,
                "status": self.status,
                "message": self.message,
            }
        )


def _async_handle_message(task_id: str, tool_name: str) -> str:
    """
    Build the LLM-facing instruction text on a fresh async handle.

    Every word here is load-bearing — the message is the LLM's
    only signal that the result is NOT in this string. Without
    "asynchronous" + "auto-deliver" + the literal task_id, the
    LLM tends to either treat the handle as the result and
    hallucinate completion, or repeatedly poll for status.

    :param task_id: The async task's ID, included verbatim so
        the LLM can pass it to ``sys_cancel_task`` if it wants
        to abort.
    :param tool_name: The dispatched tool's name.
    :returns: A compact instruction string.
    """
    return (
        f"Tool {tool_name!r} dispatched asynchronously. "
        f"The result will be auto-delivered as a system message "
        f"when ready. To abort, call sys_cancel_task with "
        f"task_id={task_id!r}."
    )


# ── Phase 6: TOOL_CALL / TOOL_RESULT / OUTPUT enforcement ───


# Builtin tools that dispatch a sub-agent by its declared
# ``tools.agents`` name. Listed here so the TOOL_CALL enforcement
# site can narrow ``tool_name`` to the sub-agent's declared name
# for ``match_tools`` matching — YAML authors think of a sub-agent
# as a tool named after its ``tools.agents`` entry, not as a call
# to the generic spawn / send builtin. Names come from the tool
# classes' ``.name()`` classmethods so a rename in one of them
# can't silently desync this constant.


def _find_latest_compaction_item(
    conv_store: ConversationStore,
    conversation_id: str,
) -> ConversationItem | None:
    """
    Return the most recently appended compaction item for a
    conversation, or ``None`` if none exists.

    Uses a descending ``limit=1`` query so only one row is read
    regardless of total conversation length.

    :param conv_store: The ConversationStore to query.
    :param conversation_id: The conversation to search,
        e.g. ``"conv_abc123"``.
    :returns: The latest compaction item, or ``None``.
    """
    page = conv_store.list_items(
        conversation_id,
        type="compaction",
        order="desc",
        limit=1,
    )
    return page.data[0] if page.data else None


@dataclass(frozen=True)
class _LoadedHistory:
    """
    Result of :func:`_load_initial_history`.

    Bundles the conversation items with metadata about the latest
    compaction so callers can make decisions (e.g. seed invalidation)
    without re-querying the store.

    :param items: History items ready for prompt construction.
        May begin with a synthetic summary pair from
        :func:`compaction_to_history_items`.
    :param last_compaction_created_at: ``created_at`` of the latest
        compaction item used as a history cursor, or ``None`` when
        no valid compaction item exists.
    """

    items: list[ConversationItem]
    last_compaction_created_at: int | None = None


def _load_initial_history(
    conv_store: ConversationStore,
    conversation_id: str,
) -> _LoadedHistory:
    """
    Load the conversation history for the start of an execution.

    When a compaction item exists, only the items AFTER the
    summary's coverage boundary are loaded — the synthetic
    summary pair replaces the older items the LLM does not need
    to see verbatim. This bounds the load to O(items since last
    compaction), not O(total conversation length).

    When no compaction item exists, the full conversation is
    loaded (existing behaviour).

    :param conv_store: The ConversationStore to query.
    :param conversation_id: The conversation to load,
        e.g. ``"conv_abc123"``.
    :returns: A :class:`_LoadedHistory` with items ready for
        prompt construction and the latest compaction timestamp.
    """
    compaction_item = _find_latest_compaction_item(conv_store, conversation_id)
    if compaction_item is None:
        return _LoadedHistory(
            items=[
                item
                for item in fetch_all_items(conv_store, conversation_id)
                if item.type not in NON_CONTENT_ITEM_TYPES
            ]
        )
    assert isinstance(compaction_item.data, CompactionData)
    # Validate the compaction item before trusting it as a cursor.
    # A broken item (empty summary, missing/bogus last_item_id) would
    # make the after= query return nothing, leaving history as just
    # the empty synthetic pair — the Omnigent thinks context is ~0 tokens
    # and never triggers compaction while the executor's real context
    # keeps growing.
    last_id = compaction_item.data.last_item_id
    if not compaction_item.data.summary or not last_id or last_id.startswith("synthetic_"):
        _logger.warning(
            "Ignoring broken compaction item %s for %s: empty summary or bogus last_item_id=%r",
            compaction_item.id,
            conversation_id,
            last_id,
        )
        return _LoadedHistory(
            items=[
                item
                for item in fetch_all_items(conv_store, conversation_id)
                if item.type not in NON_CONTENT_ITEM_TYPES
            ]
        )
    # Load after last_item_id, NOT after the compaction item itself.
    # The compaction item may be appended after additional output
    # items that the summary does not cover — using last_item_id
    # ensures those post-summary items are included.
    try:
        recent_items = fetch_all_items(
            conv_store,
            conversation_id,
            after=compaction_item.data.last_item_id,
        )
    except StaleCursorError:
        # The anchor was deleted, so "everything after it" is unrecoverable.
        # Reload the whole conversation, as a structurally broken compaction
        # item does above: a superset beats a prompt starved of history.
        _logger.warning(
            "Compaction anchor %r for %s no longer exists; loading full history",
            last_id,
            conversation_id,
        )
        return _LoadedHistory(
            items=[
                item
                for item in fetch_all_items(conv_store, conversation_id)
                if item.type not in NON_CONTENT_ITEM_TYPES
            ]
        )
    # Filter metadata items — they are not conversation content the
    # LLM should receive verbatim.
    content_items = [i for i in recent_items if i.type not in NON_CONTENT_ITEM_TYPES]
    return _LoadedHistory(
        items=compaction_to_history_items(compaction_item) + content_items,
        last_compaction_created_at=compaction_item.created_at,
    )


async def compact_conversation_now(
    *,
    task_id: str,
    conversation_id: str,
    spec: AgentSpec,
    llm_config: LLMConfig,
    instructions: str | None = None,
    tool_schemas: list[dict[str, Any]] | None = None,
    model_override: str | None = None,
    preserve_recent_window: int | None = None,
) -> CompactionResult:
    """
    Force a compaction pass for an existing conversation.

    This is the runtime entry point used by the sessions ``/compact``
    control event. It builds the same prompt input that a normal agent
    iteration would use, runs compaction with ``force=True`` so Layer 2
    summarisation is attempted even below the automatic threshold, and
    persists a compaction item when a summary is produced.

    :param task_id: Synthetic response id for this compaction operation.
    :param conversation_id: Conversation/session id to compact.
    :param spec: Agent spec bound to the conversation.
    :param llm_config: Effective LLM config used for summarisation.
    :param instructions: Optional per-request instructions.
    :param tool_schemas: Tool schemas to include in system-token budget.
    :param model_override: Optional model override. Applied to
        ``llm_config`` before counting and summarising.
    :param preserve_recent_window: Override for how many recent LLM
        response groups to keep verbatim. ``/compact`` passes ``1``;
        this is mapped to the compaction helper's inclusive boundary
        semantics below so the explicit command reduces context immediately.
        ``None`` uses the agent's configured compaction policy.
    :returns: The :class:`CompactionResult` returned by
        :func:`omnigent.runtime.compaction.compact`.
    """
    conv_store = get_conversation_store()
    loaded = _load_initial_history(conv_store, conversation_id)
    history = loaded.items
    if not history:
        return CompactionResult(messages=[], summary_metadata=None)

    effective_llm_config = _apply_request_model_override(llm_config, model_override)
    effective_llm_config = _route_bare_model_for_compaction(effective_llm_config)
    compaction_config = spec.compaction
    if preserve_recent_window is not None:
        # The compaction helper's boundary is inclusive: recent_window=1
        # protects the latest assistant/function-call boundary itself, which
        # often means preserving the entire final turn. For explicit /compact,
        # use 0 to summarize through the latest completed item while still
        # keeping the command opt-in/user-driven.
        from omnigent.spec.types import CompactionConfig

        trigger_threshold = compaction_config.trigger_threshold if compaction_config else 0.8
        compaction_config = CompactionConfig(
            trigger_threshold=trigger_threshold,
            recent_window=max(preserve_recent_window - 1, 0),
        )
    compaction_state = _CompactionState(
        context_window=None,
        last_summary=None,
        config=compaction_config,
        model=effective_llm_config.model,
        connection=effective_llm_config.connection,
        conversation_id=conversation_id,
    )
    _sys_instructions, messages, sys_tokens = _prepare_messages(
        spec,
        effective_llm_config,
        history,
        instructions,
        tool_schemas or [],
        compaction_state,
        content_cache={},
        conversation_id=conversation_id,
    )
    from omnigent.llms.context_window import get_model_context_window

    context_window = get_model_context_window(effective_llm_config.model)
    result = await compact(
        messages,
        history,
        config=compaction_state.config,
        context_window=context_window,
        system_token_budget=sys_tokens,
        model=compaction_state.model,
        task_id=task_id,
        llm_client=_get_llm_client(),
        connection=compaction_state.connection,
        runner_client=_get_runner_client_for_compaction(conversation_id),
        force=True,
        fail_on_summary_error=True,
        conversation_id=conversation_id,
    )
    if result.summary_metadata is None:
        raise OmnigentError(
            "Compaction did not produce a persisted summary. The conversation was "
            "left unchanged; check server logs for the summarization failure.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    _maybe_persist_compaction_item(
        result.summary_metadata,
        task_id,
        conversation_id,
        conv_store,
    )
    return result


def _route_bare_model_for_compaction(llm_config: LLMConfig) -> LLMConfig:
    """
    Prefix bare model ids so compaction's generic client picks the right provider.

    Normal harness execution infers the provider from the harness (e.g.
    ``claude-sdk`` → Anthropic, ``pi`` → its configured family).
    Explicit ``/compact`` instead uses the generic runtime LLM client,
    whose :func:`~omnigent.llms.routing.parse_model_string` defaults any
    prefix-less id to OpenAI — so a bare Anthropic ``claude-*`` id gets sent
    to ``api.openai.com`` and the summarization call fails with a 500 (issue
    #1950). Already-prefixed ids (``anthropic/…``, ``openai/…``) and bare
    ``gpt-*`` (correctly OpenAI) are left untouched.

    :param llm_config: Effective LLM config for the session.
    :returns: ``llm_config`` unchanged, or a copy with a provider-prefixed model.
    """
    model = llm_config.model
    if "/" in model:
        # Already provider-prefixed (e.g. "anthropic/claude-…") — trust it.
        return llm_config
    if model.startswith("claude-"):
        # Bare Anthropic id (e.g. "claude-haiku-4-5-20251001") — route to
        # Anthropic instead of the prefix-less OpenAI default.
        return replace(llm_config, model=f"anthropic/{model}")
    # ponytail: only anthropic here — the /compact failures seen
    # in the wild. Other bare non-OpenAI prefixes (deepseek-, moonshot-, …)
    # would need the same nudge if they ever surface.
    return llm_config


def _maybe_persist_compaction_item(
    summary: SummaryMetadata,
    task_id: str,
    conversation_id: str,
    conv_store: ConversationStore,
) -> None:
    """
    Persist a compaction item for the current execution, unless one
    already exists (idempotent append for crash-recovery safety).

    The ``response_id`` on the item is the task ID, which is unique
    per execution. On replay the runner re-emits any pending tail —
    the check-before-write prevents a duplicate compaction item from
    being appended.

    :param summary: The :class:`SummaryMetadata` from Layer 2.
    :param task_id: The task identifier used as the item's
        ``response_id``, e.g. ``"task_abc123"``.
    :param conversation_id: The conversation to append to,
        e.g. ``"conv_abc123"``.
    :param conv_store: The ConversationStore to append to.
    """
    # Guard: never persist a broken compaction item. An empty summary
    # or a missing last_item_id would poison the history cursor so
    # _load_initial_history returns an empty history on every
    # subsequent turn — the executor keeps running on its internal
    # session while the Omnigent thinks context is near-zero and never
    # triggers compaction again.
    if (
        not summary.text
        or not summary.last_item_id
        or summary.last_item_id.startswith("synthetic_")
    ):
        _logger.warning(
            "Skipping compaction persist for task %s: empty summary "
            "or bogus last_item_id (text=%r, last_item_id=%r)",
            task_id,
            summary.text[:80] if summary.text else None,
            summary.last_item_id,
        )
        return
    existing = conv_store.list_items(
        conversation_id,
        type="compaction",
        order="desc",
        limit=1,
    )
    if existing.data and existing.data[0].response_id == task_id:
        # Already persisted — idempotent on crash recovery replay.
        return
    conv_store.append(
        conversation_id,
        [
            NewConversationItem(
                type="compaction",
                response_id=task_id,
                data=CompactionData(
                    summary=summary.text,
                    last_item_id=summary.last_item_id,
                    model=summary.model,
                    token_count=summary.token_count,
                ),
            )
        ],
    )


# ── await_tool_output implementation ───────────────────────


# ── The agent loop ────────────────────────────────────────


def _find_spec_by_name(
    spec: AgentSpec,
    name: str,
) -> AgentSpec | None:
    """
    Resolve a sub-agent spec by name within the parent spec tree.

    Recursively searches ``spec.sub_agents`` (names are validated unique
    across the tree, so at most one matches). The root is never matched
    against itself, even when ``spec.name == name``.

    :param spec: The root agent spec to search under.
    :param name: The sub-agent name to find, e.g. ``"researcher"``.
    :returns: The matching sub-agent spec, or ``None`` if not found.
    """
    for sa in spec.sub_agents:
        if sa.name == name:
            return sa
        found = _find_spec_by_name(sa, name)
        if found is not None:
            return found
    return None
