"""What an upstream says about its own models, beyond their ids.

A LiteLLM proxy lists every model it routes at ``/v1/models``, chat or not (embedding, image and
audio deployments too), and reports per-model facts at ``/model/info``. The pickers read those
facts instead of guessing from a name: a model whose ``mode`` is not chat is left out, and one
reported without tool calling is marked so (the catalog drops it, since every harness calls
tools), and the per-token prices it reports are kept (the Light model is the cheapest one).
When the endpoint does not answer, the list is offered as is: the model call will tell.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging

import cachetools
import httpx

from omnigent.models.model_catalog import ModelEntry
from omnigent.models.model_metadata import ModelCapability
from omnigent.superchat.models.upstreams import Upstream

_logger = logging.getLogger(__name__)

#: LiteLLM ``mode`` values a conversation can run on.
CHAT_MODES = frozenset({"chat", "responses"})
_TIMEOUT_SECONDS = 10.0
_cache: cachetools.TTLCache[tuple[str, str], dict[str, dict[str, object]]] = cachetools.TTLCache(
    maxsize=64, ttl=60
)


def _model_info(
    upstream: Upstream, api_key: str, transport: httpx.BaseTransport | None
) -> dict[str, dict[str, object]]:
    """``{model id: facts}`` from the upstream's info endpoint; empty when it does not answer."""
    key = (upstream.origin, hashlib.sha256(api_key.encode()).hexdigest())
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        with httpx.Client(transport=transport, timeout=_TIMEOUT_SECONDS) as client:
            response = client.get(
                f"{upstream.origin}{upstream.models_info_path}",
                headers=upstream.auth_headers(api_key),
            )
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        # Only the exception type: its text could carry request details.
        _logger.info("model info unavailable from %s (%s)", upstream.label, type(exc).__name__)
        return {}
    rows = payload.get("data") if isinstance(payload, dict) else None
    info: dict[str, dict[str, object]] = {}
    for row in rows if isinstance(rows, list) else []:
        name = row.get("model_name") if isinstance(row, dict) else None
        facts = row.get("model_info") if isinstance(row, dict) else None
        if isinstance(name, str) and isinstance(facts, dict):
            info.setdefault(name, facts)
    _cache[key] = info
    return info


def upstream_models_with_info(
    models: tuple[ModelEntry, ...],
    upstream: Upstream,
    api_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[ModelEntry, ...]:
    """Refine an upstream's listed *models* with the facts it reports about each.

    :returns: The chat models, with tool-calling support and context size where reported; a
        model the endpoint says nothing about is kept unchanged.
    """
    if upstream.models_info_path is None:
        return models
    info = _model_info(upstream, api_key, transport)
    refined: list[ModelEntry] = []
    for entry in models:
        facts = info.get(entry.id)
        if not facts:
            refined.append(entry)
            continue
        mode = facts.get("mode")
        if isinstance(mode, str) and mode not in CHAT_MODES:
            continue
        metadata = entry.metadata
        tools = facts.get("supports_function_calling")
        if isinstance(tools, bool):
            supported = metadata.supported_capabilities - {ModelCapability.TOOL_USE}
            unsupported = metadata.unsupported_capabilities - {ModelCapability.TOOL_USE}
            if tools:
                supported |= {ModelCapability.TOOL_USE}
            else:
                unsupported |= {ModelCapability.TOOL_USE}
            metadata = dataclasses.replace(
                metadata, supported_capabilities=supported, unsupported_capabilities=unsupported
            )
        window = facts.get("max_input_tokens")
        if metadata.context_window is None and isinstance(window, int) and window > 0:
            metadata = dataclasses.replace(metadata, context_window=window)
        prices = {
            field: float(value)
            for field in ("input_cost_per_token", "output_cost_per_token")
            if isinstance(value := facts.get(field), (int, float))
            and not isinstance(value, bool)
            and value >= 0
        }
        if prices:
            metadata = dataclasses.replace(metadata, **prices)
        refined.append(dataclasses.replace(entry, metadata=metadata))
    return tuple(refined)


def clear_model_info_cache() -> None:
    """Forget cached model facts (tests)."""
    _cache.clear()
