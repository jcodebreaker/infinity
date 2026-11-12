"""The provider registry: one table describing every model upstream the engine can call.

The probe (key validation), the model proxy (where a call goes and how it is authenticated) and
the connection routes (which providers exist) all read this table; nothing else hardcodes a
provider. ``wire_apis`` is the fact harness fit is decided on (which wire protocol a harness
speaks and whether the upstream serves it), never a model's name: a model id is whatever the
upstream calls it, and the upstream's own answer says whether it serves it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from omnigent.harness_aliases import canonicalize_harness
from omnigent.models.model_override import canonical_model_spelling

AuthStyle = Literal["x-api-key", "bearer"]


@dataclass(frozen=True)
class EmbeddingRoute:
    """How an upstream serves embeddings (an OpenAI-compatible ``/embeddings`` endpoint).

    :param api_base: Base URL litellm calls (the ``/embeddings`` path is appended by litellm);
        ``None`` means the upstream's own ``<origin>/v1`` (an upstream whose origin is configured).
    :param model_prefix: Prefix that makes litellm treat the model as OpenAI-compatible
        (``openai/``); the rest of the id is what the upstream receives.
    """

    api_base: str | None = None
    model_prefix: str = "openai/"


@dataclass(frozen=True)
class Upstream:
    """One model upstream.

    :param name: Registry key, also the ``{provider}`` segment of the proxy route.
    :param label: Display name used in messages.
    :param base_url: Origin (plus any fixed prefix) the proxy forwards ``/v1/...`` paths onto;
        empty for an upstream whose origin is the deployment's own (``base_url_env``).
    :param auth: How the key is sent: an ``x-api-key`` header or ``Authorization: Bearer``.
    :param wire_apis: Wire protocols served: ``messages`` (Anthropic), ``chat`` (OpenAI Chat
        Completions), later ``responses``.
    :param probe_path: Cheap authenticated GET under ``base_url`` that answers 200 for a good key.
    :param extra_headers: Static headers every request to this upstream needs.
    :param id_mapper: Rewrites a catalog model id into the spelling this upstream expects (the
        proxy applies it before forwarding); ``None`` when ids pass through unchanged.
    :param embeddings: How this upstream serves embeddings, or ``None`` when it does not (a
        connection to it cannot embed files).
    :param base_url_env: Environment variable naming the origin, for an upstream the operator
        runs (a LiteLLM proxy); unset means the upstream is not offered on this deployment.
    :param lists_models: Whether a connection's model catalog is always read from the upstream
        (with the owner's key), even when the harness binding names a static list: the upstream's
        model ids are the deployment's own names. Any upstream is listed when the binding names no
        list.
    :param models_info_path: Authenticated GET under the origin that reports per-model facts
        (LiteLLM's ``/model/info``: mode, tool calling, context size), used to keep non-chat
        models out of the pickers; ``None`` when the upstream reports none.
    :param operator_key_env: Environment variable holding the operator's own key for an
        upstream the operator runs (a LiteLLM proxy): used, in the engine only, when neither the
        person nor the organization saved a key. ``None`` for hosted vendors, where every call is
        billed to a saved key.
    """

    name: str
    label: str
    base_url: str
    auth: AuthStyle
    wire_apis: frozenset[str]
    probe_path: str
    extra_headers: tuple[tuple[str, str], ...] = ()
    id_mapper: Callable[[str], str] | None = None
    embeddings: EmbeddingRoute | None = None
    base_url_env: str | None = None
    lists_models: bool = False
    models_info_path: str | None = None
    operator_key_env: str | None = None

    @property
    def origin(self) -> str:
        """:returns: Where calls go: ``base_url``, or the configured origin (a trailing ``/v1``
        dropped, since the routes add it), or ``""`` when an operator-run upstream is unset."""
        if self.base_url_env is None:
            return self.base_url
        configured = os.environ.get(self.base_url_env, "").strip().rstrip("/")
        return configured.removesuffix("/v1").rstrip("/")

    @property
    def configured(self) -> bool:
        """:returns: Whether this deployment can call the upstream (it has an origin)."""
        return bool(self.origin)

    @property
    def embeddings_api_base(self) -> str | None:
        """:returns: The base URL embedding calls go to, or ``None`` when it cannot embed."""
        if self.embeddings is None:
            return None
        return self.embeddings.api_base or f"{self.origin}/v1"

    @property
    def operator_key(self) -> str | None:
        """:returns: The operator's key for this upstream from the environment, if it has one."""
        if self.operator_key_env is None:
            return None
        return os.environ.get(self.operator_key_env, "").strip() or None

    @property
    def lists_with_anthropic_api(self) -> bool:
        """:returns: Whether its model list is the Anthropic models API (an upstream that speaks
        only Messages) rather than an OpenAI-compatible ``/v1/models``."""
        return "chat" not in self.wire_apis

    def serves_wire(self, wire: str) -> bool:
        """:returns: Whether this upstream serves the wire protocol *wire*."""
        return wire in self.wire_apis

    def llm_route(self, model: str) -> tuple[str, str]:
        """How the engine itself calls *model* on this upstream (background work).

        Decided by the upstream's wire and auth, never the model's name: an ``x-api-key``
        upstream that speaks Messages is called as Anthropic; a bearer upstream over its Chat
        Completions.

        :returns: ``(model string for the LLM client, base URL)``.
        """
        if self.auth == "x-api-key" and "messages" in self.wire_apis:
            return f"anthropic/{self.upstream_model(model)}", f"{self.origin}/v1"
        return f"openai_compatible/{self.upstream_model(model)}", f"{self.origin}/v1"

    def upstream_model(self, model: str) -> str:
        """:returns: *model* spelled the way this upstream expects it."""
        return self.id_mapper(model) if self.id_mapper else model

    def key_header(self, api_key: str) -> dict[str, str]:
        """:returns: The one header that carries *api_key* to this upstream."""
        if self.auth == "x-api-key":
            return {"x-api-key": api_key}
        return {"Authorization": f"Bearer {api_key}"}

    def auth_headers(self, api_key: str) -> dict[str, str]:
        """:returns: Every header a standalone request (the probe) needs."""
        return {**dict(self.extra_headers), **self.key_header(api_key)}

    @property
    def probe_url(self) -> str:
        """:returns: The full URL of the key probe."""
        return f"{self.origin}{self.probe_path}"


# Bare dated suffix ("-20250514") and a trailing "<major>-<minor>" or leading "<major>-<minor>-".
_DATE_SUFFIX = re.compile(r"-\d{8}$")
_CLAUDE_VERSION_AFTER = re.compile(r"^(claude-[a-z]+)-(\d{1,2})-(\d{1,2})$")
_CLAUDE_VERSION_BEFORE = re.compile(r"^claude-(\d{1,2})-(\d{1,2})-([a-z]+)$")


def openrouter_model_id(model: str) -> str:
    """Spell a catalog model id the way OpenRouter does.

    OpenRouter ids are vendor-prefixed with dotted versions (``anthropic/claude-sonnet-4.5``)
    while the catalog's Claude ids are bare and dashed (``claude-sonnet-4-5``). A bare Claude id
    (a ``databricks-`` gateway prefix and a date suffix are dropped first) becomes
    ``anthropic/claude-<name>-<major>.<minor>`` (or ``claude-<major>.<minor>-<name>`` for the 3.x
    shape); any other id, including one already carrying a ``/``, passes through unchanged.
    """
    bare = canonical_model_spelling(model)
    if "/" in bare or not bare.startswith("claude-"):
        return model
    bare = _DATE_SUFFIX.sub("", bare)
    if m := _CLAUDE_VERSION_AFTER.match(bare):
        bare = f"{m[1]}-{m[2]}.{m[3]}"
    elif m := _CLAUDE_VERSION_BEFORE.match(bare):
        bare = f"claude-{m[1]}.{m[2]}-{m[3]}"
    return f"anthropic/{bare}"


UPSTREAMS: dict[str, Upstream] = {
    "anthropic": Upstream(
        name="anthropic",
        label="Anthropic",
        base_url="https://api.anthropic.com",
        auth="x-api-key",
        wire_apis=frozenset({"messages"}),
        probe_path="/v1/models",
        extra_headers=(("anthropic-version", "2023-06-01"),),
    ),
    "openrouter": Upstream(
        name="openrouter",
        label="OpenRouter",
        # Its Anthropic-compatible Messages API lives at /api/v1/messages.
        base_url="https://openrouter.ai/api",
        auth="bearer",
        wire_apis=frozenset({"messages", "chat"}),
        probe_path="/v1/key",
        id_mapper=openrouter_model_id,
        embeddings=EmbeddingRoute(api_base="https://openrouter.ai/api/v1"),
    ),
    # The operator's own LiteLLM proxy (a company gateway): OpenAI-compatible, plus the Anthropic
    # Messages passthrough LiteLLM serves at /v1/messages. Model ids are the proxy's own names,
    # so the model list comes from it. Offered only when LITELLM_BASE_URL is set; the operator's
    # LITELLM_API_KEY serves everyone who saved no key of their own (it stays in the engine).
    "litellm": Upstream(
        name="litellm",
        label="LiteLLM",
        base_url="",
        base_url_env="LITELLM_BASE_URL",
        auth="bearer",
        wire_apis=frozenset({"messages", "chat"}),
        probe_path="/v1/models",
        lists_models=True,
        models_info_path="/model/info",
        embeddings=EmbeddingRoute(),
        operator_key_env="LITELLM_API_KEY",
    ),
    # OpenAI is deliberately not wired yet (needs the "responses" wire API):
    # "openai": Upstream("openai", "OpenAI", "https://api.openai.com", "bearer",
    #                    frozenset({"responses", "chat"}), "any", "/v1/models"),
}
PROVIDERS = tuple(UPSTREAMS)


def harness_family(harness: str) -> str:
    """:returns: The vendor family a harness declares in its capabilities (``claude``, ``gpt``
    or ``multi``); ``multi`` when the harness declares nothing."""
    from omnigent.harness_plugins import harness_capabilities

    capabilities = harness_capabilities()
    caps = capabilities.get(harness) or capabilities.get(canonicalize_harness(harness) or harness)
    return caps.model_family.value if caps is not None else "multi"


# Providers, best first; a later slice may order claude and multi differently.
_PREFERENCE = ("anthropic", "openrouter", "litellm")

#: The wire protocol a harness family speaks through a model connection: the Claude family
#: speaks Anthropic Messages, the GPT family Chat Completions (until ``responses`` is
#: wired), a multi-model harness either.
_FAMILY_WIRES: dict[str, frozenset[str]] = {
    "claude": frozenset({"messages"}),
    "gpt": frozenset({"chat"}),
}


def preferred_providers(family: str) -> tuple[str, ...]:
    """:returns: Providers to try, best first, among those whose wire *family* speaks.

    Decided by the upstream's wire protocols: a Messages-only upstream (Anthropic) serves the
    ``claude`` and ``multi`` families, not a harness that only speaks Chat Completions.
    """
    wires = _FAMILY_WIRES.get(family)
    return tuple(
        name for name in _PREFERENCE if wires is None or wires & UPSTREAMS[name].wire_apis
    )


def connection_upstreams(provider: object, family: str) -> tuple[str, ...]:
    """The upstreams a ``connection: model`` provider entry may use for a harness *family*,
    best first: :func:`preferred_providers`, narrowed by the entry's optional ``upstreams``
    list (a deployment that routes every call through its LiteLLM proxy names only
    ``litellm``)."""
    preferred = preferred_providers(family)
    only = provider.get("upstreams") if isinstance(provider, dict) else None
    if not isinstance(only, list):
        return preferred
    return tuple(name for name in preferred if name in only)


def offered_providers(only: Iterable[str] | None = None) -> list[dict[str, object]]:
    """:returns: The providers a key can be saved for on this deployment, best first:
    ``[{"provider", "label", "operator_key"}]`` (an operator-run upstream only when it is
    configured; ``operator_key`` when the operator's own key serves people without one).

    :param only: The upstreams the deployment's connections may use, when it narrows them.
    """
    allowed = set(only) if only is not None else None
    return [
        {
            "provider": name,
            "label": UPSTREAMS[name].label,
            "operator_key": UPSTREAMS[name].operator_key is not None,
        }
        for name in PROVIDERS
        if UPSTREAMS[name].configured and (allowed is None or name in allowed)
    ]
