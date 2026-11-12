"""Owner-scoped model discovery for immutable managed-sandbox inference profiles."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
from typing import Any
from urllib.parse import urlsplit

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.inference_config import (
    MODEL_PROXY_AUTH_COMMAND,
    HarnessInferenceBinding,
    _is_model_connection,
    binding_for_harness,
    inference_revision,
    normalize_inference_harness,
    parse_inference_config,
    resolve_bound_provider,
    validate_inference_credentials,
)
from omnigent.models import model_catalog
from omnigent.models.model_catalog import (
    ModelEntry,
    ResolvedModelProvider,
    model_family_conflict,
    model_family_token,
)
from omnigent.models.model_metadata import ModelCapability, ModelWireAPI
from omnigent.onboarding.provider_config import (
    MODEL_CONNECTION,
    ProviderEntry,
    load_providers,
    resolve_secret,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.superchat.models.listing import upstream_models_with_info
from omnigent.superchat.models.proxy import KEY_REQUIRED_MESSAGE
from omnigent.superchat.models.store import resolve_model_connection
from omnigent.superchat.models.upstreams import UPSTREAMS, connection_upstreams, harness_family


def _invalid(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid("Inference endpoints must be literal HTTP or HTTPS URLs.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "$" in value
    ):
        raise _invalid("Inference endpoints must be literal URLs without credentials or queries.")
    return value.rstrip("/")


def _family(provider: ProviderEntry, harness: str, model: str | None = None) -> str:
    canonical = canonicalize_harness(normalize_inference_harness(harness))
    if canonical == "claude-sdk":
        family = "anthropic"
    elif canonical == "pi":
        preferred = (
            ("openai", "anthropic")
            if model and "claude" not in model.lower()
            else ("anthropic", "openai")
        )
        family = next((name for name in preferred if name in provider.families), "")
    else:
        raise _invalid(f"Harness {harness!r} does not support managed inference profiles.")
    if family not in provider.families:
        raise _invalid(f"Provider {provider.name!r} has no {family} endpoint for {harness!r}.")
    return family


def _wire(provider: ProviderEntry, harness: str, model: str | None = None) -> ModelWireAPI:
    family = _family(provider, harness, model)
    if family == "anthropic":
        return ModelWireAPI.ANTHROPIC_MESSAGES
    protocol = provider.families[family].wire_api
    return ModelWireAPI.OPENAI_CHAT if protocol == "chat" else ModelWireAPI.OPENAI_RESPONSES


def _alias(model: str, tiers: dict[str, str]) -> str:
    seen: set[str] = set()
    while model in tiers and tiers[model] != model:
        if model in seen:
            raise _invalid("The configured provider's model aliases contain a cycle.")
        seen.add(model)
        model = tiers[model]
    return model


def snapshot_uses_model_proxy(snapshot: dict[str, Any] | None) -> bool:
    """Whether the session's harness reaches its model through the engine's model proxy.

    Such a session's spend is recorded by the proxy (the one source of truth for its traffic), so
    the per-turn pricing in the session relay must not add it again.
    """
    if not snapshot or not isinstance(snapshot.get("runtime_config"), dict):
        return False
    runtime = snapshot["runtime_config"]
    try:
        binding = binding_for_harness(runtime, snapshot["harness"])
    except (KeyError, ValueError):
        return False
    provider = runtime.get("providers", {}).get(binding.provider) if binding else None
    return isinstance(provider, dict) and any(
        isinstance(family, dict) and family.get("auth_command") == MODEL_PROXY_AUTH_COMMAND
        for family in provider.values()
    )


def _binding_key(config: dict[str, Any], harness: str) -> str:
    canonical = normalize_inference_harness(harness)
    for key in config["inference"]["harnesses"]:
        if normalize_inference_harness(key) == canonical:
            return key
    return harness


def _resolve_alias(provider: ProviderEntry, harness: str, model: str) -> str:
    """Resolve across supported families, rejecting conflicting tier definitions."""
    canonical = canonicalize_harness(normalize_inference_harness(harness))
    families = (
        [name for name in ("anthropic", "openai") if name in provider.families]
        if canonical in {"pi", "pi-native"}
        else [_family(provider, harness)]
    )
    resolved = {
        _alias(model, provider.families[name].models)
        for name in families
        if model in provider.families[name].models
    }
    if len(resolved) > 1:
        raise _invalid(
            f"Model alias {model!r} is ambiguous across provider families. Use an exact model ID."
        )
    return next(iter(resolved), model)


def _unfit(harness: str, model: str, *, connection: bool) -> bool:
    """Whether *model* cannot run on *harness*.

    A model connection's ids are its upstream's own names (``claude-team`` may be anything): it
    is never judged by name. The harness reaches it over a wire the upstream serves (checked when
    the snapshot is prepared), the listing's own metadata filters the rest, and an id the
    upstream cannot serve is answered by the upstream. An operator gateway's curated ids keep the
    name rule for ids that recognizably belong to another vendor.
    """
    return not connection and model_family_conflict(harness, model) is not None


def catalog_row_details(snapshot: dict[str, Any], model: str) -> dict[str, Any]:
    """Wire API, family token and configured pricing for one catalog row of a saved snapshot."""
    config = snapshot["runtime_config"]
    harness = snapshot["harness"]
    binding = binding_for_harness(config, harness)
    details: dict[str, Any] = {
        "family": model_family_token(model),
        "wire_api": None,
        "pricing": None,
    }
    if binding is None:
        return details
    provider = load_providers(config)[binding.provider]
    try:
        details["wire_api"] = _wire(provider, harness, model).value
        family = provider.families[_family(provider, harness, model)]
    except OmnigentError:
        return details
    if family.pricing is not None:
        details["pricing"] = dataclasses.asdict(family.pricing)
    return details


class SandboxInferenceService:
    """Load a static target once, then discover through its saved configuration."""

    def __init__(self, app_state: Any, *, transport: httpx.BaseTransport | None = None) -> None:
        self._state = app_state
        self._transport = transport

    async def prepare(
        self,
        provider: str | None,
        harness: str,
        user_id: str | None,
        agent_auth: object = None,
    ) -> dict[str, Any] | None:
        """Capture nonsecret launch inputs and verify the selected provider's catalog."""
        deployment = getattr(self._state, "sandbox_config", None)
        if deployment is None:
            return None
        target = deployment.for_provider(provider)
        if target is None:
            offered = ", ".join(deployment.launchable_providers()) or "none"
            raise _invalid(
                f"host provider '{provider}' is not configured "
                f"on this server — available: {offered}"
            )
        raw: dict[str, Any] = copy.deepcopy(target.host_config or {})
        if not parse_inference_config(raw):
            return None
        harness = normalize_inference_harness(harness)
        discovery = copy.deepcopy(getattr(target, "model_discovery", None) or {})
        try:
            validate_inference_credentials(raw, discovery)
        except ValueError:
            raise _invalid(
                "Inference credentials must use supported api_key_ref references or "
                "auth_command; inline keys are not allowed."
            ) from None
        for discovery_entry in discovery.values():
            if not isinstance(discovery_entry, dict):
                raise _invalid("Model discovery entries must be mappings.")
            _endpoint(discovery_entry.get("base_url"))
        bound = resolve_bound_provider(raw, harness, agent_auth, allow_empty=True)
        owner = user_id or RESERVED_USER_LOCAL
        runtime: dict[str, Any] = {
            "providers": copy.deepcopy(raw.get("providers", {})),
            "inference": copy.deepcopy(raw.get("inference", {})),
        }
        # ``connection: model`` gateways: the owner's saved key stays in the engine; the host
        # is pointed at the engine's model proxy and authenticates with its own launch token.
        model_names = [
            name
            for name, entry in load_providers(raw).items()
            if entry.kind == "gateway" and entry.connection == MODEL_CONNECTION
        ]
        model_providers: dict[str, str] = {}
        if model_names:
            model_store = getattr(self._state, "model_connection_store", None)
            # The person's key, else the organization's, else an operator-run upstream's own key,
            # among the upstreams the bound connection entry may use.
            entry_name = bound.name if bound is not None and bound.name in model_names else None
            saved = await asyncio.to_thread(
                resolve_model_connection,
                model_store,
                owner_id=owner,
                preferred=connection_upstreams(
                    raw["providers"][entry_name or model_names[0]], harness_family(harness)
                ),
            )
            if saved is None:
                if bound is not None and bound.name in model_names:
                    raise OmnigentError(KEY_REQUIRED_MESSAGE, code=ErrorCode.MODEL_KEY_REQUIRED)
            else:
                proxy_url = _endpoint(f"{target.server_url.rstrip('/')}/v1/model/{saved.provider}")
                upstream = UPSTREAMS[saved.provider]
                # Anthropic Messages at the proxy root serves claude-sdk (and pi's Claude ids).
                # An upstream that also speaks Chat Completions (OpenRouter, LiteLLM) serves any
                # other model there, which pi uses at ``<proxy>/v1`` (pi appends
                # ``/chat/completions``): the openai family.
                for name in model_names:
                    model_providers[name] = saved.provider
                    entry_raw = raw["providers"][name]
                    keep = {"models", "pricing", "context_window", "max_output_tokens"}
                    families = {"anthropic": {"base_url": proxy_url}}
                    if "chat" in upstream.wire_apis:
                        families["openai"] = {"base_url": f"{proxy_url}/v1", "wire_api": "chat"}
                    # The connection's models are listed from the upstream with the owner's key
                    # at catalog time (the key never enters the snapshot): always for an upstream
                    # whose ids are the deployment's own names, and whenever the binding names no
                    # static list of its own.
                    own_binding = binding_for_harness(raw, harness)
                    unlisted = own_binding is None or own_binding.model_allowlist is None
                    if (upstream.lists_models or unlisted) and name not in discovery:
                        discovery[name] = {
                            "connection": MODEL_CONNECTION,
                            "upstream": upstream.name,
                        }
                    runtime["providers"][name] = {
                        "kind": "gateway",
                        "default": entry_raw.get("default", False),
                        "display_name": entry_raw.get("display_name"),
                        **{
                            family_name: {
                                **{
                                    key: value
                                    for key, value in entry_raw.get(family_name, {}).items()
                                    if key in keep
                                },
                                **extra,
                                "auth_command": MODEL_PROXY_AUTH_COMMAND,
                            }
                            for family_name, extra in families.items()
                        },
                    }
        providers = load_providers(runtime)
        if bound is not None and providers[bound.name].kind not in {"gateway", "key", "local"}:
            raise _invalid("Managed inference requires a gateway, API key, or local provider.")
        for name in providers:
            for family_name in ("anthropic", "openai"):
                original_family = raw["providers"][name].get(family_name, {})
                if original_family.get("api_key") is not None:
                    raise _invalid("Keep inference credentials in api_key_ref or auth_command.")
                if original_family.get("base_url") is not None:
                    _endpoint(original_family["base_url"])
            for family in providers[name].families.values():
                _endpoint(family.base_url)
        for saved_harness, saved in runtime["inference"]["harnesses"].items():
            entry = providers[saved["provider"]]
            if not entry.families:
                continue
            if saved.get("model_allowlist") is not None:
                saved["model_allowlist"] = list(
                    dict.fromkeys(
                        _resolve_alias(entry, saved_harness, model)
                        for model in saved["model_allowlist"]
                    )
                )
            if saved.get("default_model"):
                saved["default_model"] = _resolve_alias(
                    entry, saved_harness, saved["default_model"]
                )
        target_id = f"sandbox:{target.provider or 'default'}"
        snapshot: dict[str, Any] = {
            "version": 1,
            "target_id": target_id,
            "configuration_revision": inference_revision(
                raw,
                {
                    "model_discovery": discovery,
                    "model_providers": model_providers,
                    "target_id": target_id,
                    "harness": harness,
                },
            ),
            "harness": harness,
            "owner_id": owner,
            "runtime_config": runtime,
            "model_discovery": discovery,
        }
        preview = await self.catalog(snapshot)
        snapshot["catalog"] = preview
        if preview["status"] == "ready":
            saved_binding = runtime["inference"]["harnesses"][_binding_key(runtime, harness)]
            saved_binding["default_model"] = preview["default_model"]
        return copy.deepcopy(snapshot)

    def _connection_models(
        self, snapshot: dict[str, Any], discovery: dict[str, Any]
    ) -> tuple[ModelEntry, ...]:
        """List the models a ``connection: model`` upstream serves, with the owner's saved key."""
        upstream = UPSTREAMS.get(str(discovery.get("upstream")))
        store = getattr(self._state, "model_connection_store", None)
        if upstream is None or not upstream.configured:
            raise _invalid("This model connection's provider is not configured on this server.")
        saved = resolve_model_connection(
            store, owner_id=snapshot["owner_id"], preferred=(upstream.name,)
        )
        if saved is None:
            raise OmnigentError(KEY_REQUIRED_MESSAGE, code=ErrorCode.MODEL_KEY_REQUIRED)
        # A Messages-only upstream lists through the Anthropic models API; any other through its
        # OpenAI-compatible /v1/models.
        anthropic_api = upstream.lists_with_anthropic_api
        descriptor = ResolvedModelProvider(
            kind="key" if anthropic_api else "gateway",
            family="anthropic" if anthropic_api else "openai",
            base_url=upstream.origin if anthropic_api else f"{upstream.origin}/v1",
            api_key=saved.plaintext,
            detail=f"connection:{upstream.name}:{saved.scope}:{snapshot['owner_id']}",
        )
        listing = model_catalog._listing_for_provider(descriptor, transport=self._transport)
        if not listing.verified:
            raise _invalid(f"Could not list models from {upstream.label}. Check the connection.")
        if upstream.models_info_path is None:
            return listing.models
        return upstream_models_with_info(
            listing.models, upstream, saved.plaintext, transport=self._transport
        )

    def _gateway_models(
        self, snapshot: dict[str, Any], provider: ProviderEntry
    ) -> tuple[ModelEntry, ...]:
        discovery = snapshot.get("model_discovery", {}).get(provider.name)
        if not isinstance(discovery, dict):
            raise _invalid(
                f"Configure host.model_discovery.{provider.name} for server-side model discovery."
            )
        if discovery.get("connection") == MODEL_CONNECTION:
            return self._connection_models(snapshot, discovery)
        base_url = _endpoint(discovery.get("base_url"))
        secret_ref = discovery.get("api_key_ref")
        family = discovery.get("family", "openai")
        descriptor = ResolvedModelProvider(
            kind="key" if family == "anthropic" else "gateway",
            family=family,
            base_url=base_url,
            api_key=resolve_secret(secret_ref) if secret_ref else None,
            auth_command=discovery.get("auth_command"),
            detail=f"sandbox:{snapshot['configuration_revision']}:{snapshot['owner_id']}:{provider.name}",
        )
        listing = model_catalog._listing_for_provider(descriptor, transport=self._transport)
        if not listing.verified:
            raise OmnigentError(
                "Model discovery is unavailable. Check the gateway and discovery credential.",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        return listing.models

    def _static_catalog(
        self,
        result: dict[str, Any],
        binding: HarnessInferenceBinding,
        provider: ProviderEntry,
        harness: str,
        *,
        connection: bool = False,
    ) -> dict[str, Any]:
        """Serve the operator's curated list when no server discovery is configured.

        Trusts ``model_allowlist`` instead of listing the gateway: no server-side
        credential is resolved and no public catalog is consulted. Availability
        errors surface when the harness uses the selected model.
        """
        if binding.model_allowlist is None:
            result["error"] = (
                "Configure host.model_discovery for this provider "
                "or a model_allowlist for this harness."
            )
            return result
        ids = [m for m in binding.model_allowlist if not _unfit(harness, m, connection=connection)]
        if not ids:
            result.update(status="empty", error="No models are permitted for this harness.")
            return result
        default = binding.default_model or ids[0]
        if default not in ids:
            raise _invalid("The configured default model is not in the harness model list.")
        source = {"kind": provider.kind, "label": result["provider_label"], "name": provider.name}
        result.update(
            status="ready",
            default_model=default,
            default_configured=binding.default_model is not None,
            models=[
                {
                    "id": model,
                    "model": model,
                    "displayName": model,
                    "isDefault": model == default,
                    "source": source,
                }
                for model in ids
            ],
        )
        return result

    async def catalog(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Recheck saved owner/endpoint/model policy without reading the latest target."""
        result: dict[str, Any] = {
            "models": [],
            "configured": True,
            "configuration_revision": snapshot["configuration_revision"],
            "provider_label": None,
            "default_model": None,
            "status": "unavailable",
        }
        try:
            config = snapshot["runtime_config"]
            harness = snapshot["harness"]
            binding = binding_for_harness(config, harness)
            if binding is None:
                result.update(configured=False, status="unconfigured")
                return result
            provider = load_providers(config)[binding.provider]
            result["provider_label"] = (
                config["providers"][provider.name].get("display_name") or provider.name
            )
            result["default_model"] = binding.default_model
            _wire(provider, harness)
            connection = _is_model_connection(config["providers"][provider.name])
            if not isinstance(snapshot.get("model_discovery", {}).get(provider.name), dict):
                return self._static_catalog(
                    result, binding, provider, harness, connection=connection
                )
            entries = await asyncio.to_thread(self._gateway_models, snapshot, provider)
            available = {
                entry.id: entry
                for entry in entries
                if not _unfit(harness, entry.id, connection=connection)
                and (
                    not entry.metadata.wire_apis
                    or _wire(provider, harness, entry.id) in entry.metadata.wire_apis
                )
                and entry.metadata.supports(ModelCapability.TOOL_USE) is not False
            }
            ids = (
                list(available)
                if binding.model_allowlist is None
                else [model for model in binding.model_allowlist if model in available]
            )
            if not ids:
                result.update(
                    status="empty",
                    error="No usable models match this harness's configured model list.",
                )
                return result
            default = binding.default_model
            configured = default is not None
            if default is not None and default not in ids:
                raise _invalid("The configured default model is unavailable for this harness.")
            if default is None:
                family = provider.families[_family(provider, harness)]
                candidate = (
                    _alias(family.default_model, family.models) if family.default_model else None
                )
                configured = candidate in ids
                default = candidate if configured else ids[0]
            source = {
                "kind": provider.kind,
                "label": result["provider_label"],
                "name": provider.name,
            }
            rows = []
            effort_order = {
                name: i
                for i, name in enumerate(
                    ("none", "minimal", "low", "medium", "high", "xhigh", "max")
                )
            }
            for model in ids:
                row: dict[str, Any] = {
                    "id": model,
                    "model": model,
                    "displayName": model,
                    "isDefault": model == default,
                    "source": source,
                }
                metadata = available[model].metadata
                if metadata.input_cost_per_token is not None:
                    row["costPerToken"] = {
                        "input": metadata.input_cost_per_token,
                        "output": metadata.output_cost_per_token,
                    }
                reasoning = metadata.reasoning
                if reasoning and reasoning.efforts:
                    row["supportedReasoningEfforts"] = [
                        {"reasoningEffort": effort}
                        for effort in sorted(
                            reasoning.efforts, key=lambda e: (effort_order.get(e, 99), e)
                        )
                    ]
                rows.append(row)
            result.update(
                status="ready", models=rows, default_model=default, default_configured=configured
            )
        except OmnigentError as exc:
            result["error"] = exc.message
            # Discovery that could not reach the provider: an outage to retry, not bad input.
            result["retryable"] = exc.code == ErrorCode.RUNNER_UNAVAILABLE
        except Exception:  # noqa: BLE001 - discovery failures expose no credential or upstream body
            result["error"] = (
                "Model discovery is unavailable. Check the provider connection and catalog access."
            )
            result["retryable"] = True
        return result
