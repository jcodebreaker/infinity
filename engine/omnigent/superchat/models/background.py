"""The model background work runs on: the owner's Light model, over the owner's connection.

Memory upkeep, the daily note, Dreaming and a summary written while a session's runner is away
are model calls the engine makes itself. They run on the owner's Light model (their pick in
Settings, Models, else the cheapest model their catalog serves; see
:mod:`omnigent.superchat.models.tiers`), never a name from configuration. The call goes through
the same provider the Muse's harness is bound to: for a model connection, straight to the
upstream with the owner's key (theirs, else the organization's, else the operator's for an
upstream the operator runs), over the upstream's own wire; for an operator gateway, through its
configured family.

Registered with :func:`omnigent.llms.background.register_background_llm` by the models feature.
With no harness binding, no connection or an empty catalog there is nothing to call: the work is
skipped, and Settings shows the "pick a model" state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import cachetools

from omnigent.inference_config import (
    _is_model_connection,
    binding_for_harness,
    parse_inference_config,
)
from omnigent.llms.background import BackgroundLLM, provider_llm_route
from omnigent.server.auth import RESERVED_USER_WORKSPACE
from omnigent.superchat.models.selection import ModelPicks
from omnigent.superchat.models.store import resolve_model_connection
from omnigent.superchat.models.tiers import person_models
from omnigent.superchat.models.upstreams import UPSTREAMS, connection_upstreams, harness_family

_logger = logging.getLogger(__name__)
_CACHE_SECONDS = 60


def background_harness(bindings: list[str], picks: ModelPicks) -> str | None:
    """The harness whose models background work uses: the first bound harness the person
    picked a Light model for, else one they picked a Foundation for, else the first bound one."""
    for chosen in (picks.light, picks.foundation):
        for harness in bindings:
            if chosen.get(harness):
                return harness
    return bindings[0] if bindings else None


class BackgroundModels:
    """Resolves the background model and connection for an owner (cached briefly)."""

    def __init__(self, app_state: Any) -> None:
        self._state = app_state
        self._cache: cachetools.TTLCache[tuple[str, str], BackgroundLLM | None] = (
            cachetools.TTLCache(maxsize=256, ttl=_CACHE_SECONDS)
        )
        self._light: cachetools.TTLCache[str, str | None] = cachetools.TTLCache(
            maxsize=256, ttl=_CACHE_SECONDS
        )

    def _host_config(self) -> dict[str, Any] | None:
        deployment = getattr(self._state, "sandbox_config", None)
        target = deployment.for_provider(None) if deployment is not None else None
        return (target.host_config or None) if target is not None else None

    async def resolve(
        self, owner_id: str | None, category: str | None = None
    ) -> BackgroundLLM | None:
        """``(model string, connection)`` for *owner_id*'s background work (their Light), or
        their pick of *category* (``builder``: the Lookout's Judge) on the same harness, or
        ``None``."""
        owner = owner_id or RESERVED_USER_WORKSPACE
        key = (owner, category or "light")
        if key in self._cache:
            return self._cache[key]
        resolved = await self._resolve(owner, key[1])
        self._cache[key] = resolved
        return resolved

    async def for_session(
        self, owner_id: str | None, snapshot: dict[str, Any] | None, *, model: str | None = None
    ) -> BackgroundLLM | None:
        """*owner_id*'s Light model for the session's own harness, or *model*, called over the
        session's own provider connection (its inference *snapshot*): the fold never sends a
        Conversation's words to another provider, tenant or region than the session's.

        ``None`` when the snapshot names no bound harness or nothing can be called.
        """
        owner = owner_id or RESERVED_USER_WORKSPACE
        harness = (snapshot or {}).get("harness")
        raw = (snapshot or {}).get("runtime_config")
        if not isinstance(harness, str) or not isinstance(raw, dict):
            return None
        chosen = model or (await person_models(self._state, owner, harness)).light
        return await self._route(owner, raw, harness, chosen) if chosen else None

    async def light_model(self, owner_id: str | None) -> str | None:
        """The model id *owner_id*'s background work runs on (their Light), or ``None``."""
        owner = owner_id or RESERVED_USER_WORKSPACE
        if owner in self._light:
            return self._light[owner]
        harness = await self._harness(owner)
        model = (await person_models(self._state, owner, harness)).light if harness else None
        self._light[owner] = model
        return model

    async def _harness(self, owner: str) -> str | None:
        raw = self._host_config()
        bindings = list(parse_inference_config(raw)) if raw else []
        if not bindings:
            return None
        store = getattr(self._state, "model_preference_store", None)
        picks = await asyncio.to_thread(store.read, owner) if store is not None else ModelPicks()
        return background_harness(bindings, picks)

    async def _resolve(self, owner: str, category: str = "light") -> BackgroundLLM | None:
        harness = await self._harness(owner)
        if harness is None:
            return None
        model = (await person_models(self._state, owner, harness)).model(category)
        if not model:
            return None
        raw = self._host_config()
        assert raw is not None
        return await self._route(owner, raw, harness, model)

    async def _route(
        self, owner: str, raw: dict[str, Any], harness: str, model: str
    ) -> BackgroundLLM | None:
        """How to call *model* over *harness*'s binding in the inference config *raw*."""
        binding = binding_for_harness(raw, harness)
        if binding is None:
            return None
        entry = raw.get("providers", {}).get(binding.provider)
        if _is_model_connection(entry):
            connection = await asyncio.to_thread(
                resolve_model_connection,
                getattr(self._state, "model_connection_store", None),
                owner_id=owner,
                preferred=connection_upstreams(entry, harness_family(harness)),
            )
            if connection is None:
                return None
            model_string, base_url = UPSTREAMS[connection.provider].llm_route(model)
            return model_string, {"api_key": connection.plaintext, "base_url": base_url}
        from omnigent.onboarding.provider_config import load_providers

        provider = load_providers(raw).get(binding.provider)
        return provider_llm_route(provider, model) if provider is not None else None
