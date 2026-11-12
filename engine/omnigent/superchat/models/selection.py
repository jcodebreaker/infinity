"""Model selection: the caller's catalog per harness and their Foundation, Builder and Light picks.

``GET /v1/me/models?harness=<h>`` lists what the caller's connection serves on that harness (the
same catalog a new managed session validates against), each row with the categories it is.
``GET``/``PUT /v1/me/model-preferences`` keep the person's picks per harness in the generic
``preferences`` table (``defaults`` is accepted as Foundation for one release). A new session
picks: explicit model, then the person's Foundation (:mod:`omnigent.superchat.models.tiers`).
Settings is the source of truth: a Foundation change re-resolves the caller's Super Chat model.
``GET /v1/me/model-providers`` lists the providers a key can be saved for on this deployment.
``GET /v1/sessions/{id}/helper-models?harness=<h>`` answers a host's runner which model each helper
category runs on for the session's owner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inference_config import normalize_inference_harness
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import (
    get_user_id,
    require_access_and_level,
    require_user,
)
from omnigent.superchat.models.tiers import CATEGORIES

_logger = logging.getLogger(__name__)

PREFERENCE_KEY = "model_defaults"


def _model_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v}


@dataclass(frozen=True)
class ModelPicks:
    """A person's own picks, ``{harness: model}`` per category."""

    foundation: dict[str, str] = field(default_factory=dict)
    builder: dict[str, str] = field(default_factory=dict)
    light: dict[str, str] = field(default_factory=dict)

    def for_harness(self, harness: str) -> dict[str, str | None]:
        """``{category: model | None}`` on *harness*."""
        return {category: getattr(self, category).get(harness) for category in CATEGORIES}

    def as_body(self) -> dict[str, dict[str, str]]:
        return {category: getattr(self, category) for category in CATEGORIES}


def _picks_from(value: Any) -> ModelPicks:
    """Read a stored row: ``{category: map}``; ``defaults`` (before Builder) is Foundation; a row
    written before Light is a bare ``{harness: model}`` map of Foundation picks."""
    if not isinstance(value, dict):
        return ModelPicks()
    if any(key in value for key in (*CATEGORIES, "defaults")):
        return ModelPicks(
            foundation=_model_map(value.get("foundation", value.get("defaults"))),
            builder=_model_map(value.get("builder")),
            light=_model_map(value.get("light")),
        )
    return ModelPicks(foundation=_model_map(value))


class ModelPreferenceStore:
    """A person's Foundation, Builder and Light picks per harness, one JSON row in
    ``preferences``: ``{"foundation": {harness: model}, "builder": {...}, "light": {...}}``."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        prefix = "omnigent.model_preferences"
        self._session = make_named_managed_session_maker(engine, query_name_prefix=prefix)
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix=prefix, immediate=True
        )

    def read(self, owner: str) -> ModelPicks:
        """The person's picks; empty when never chosen or unreadable."""
        with self._session("select_model_defaults") as session:
            row = session.get(SqlPreference, (current_workspace_id(), owner, PREFERENCE_KEY))
            raw = row.value if row is not None else None
        try:
            return _picks_from(json.loads(raw) if raw else {})
        except ValueError:
            return ModelPicks()

    def set(
        self,
        owner: str,
        *,
        foundation: dict[str, str] | None = None,
        builder: dict[str, str] | None = None,
        light: dict[str, str] | None = None,
    ) -> None:
        """Replace each category's map that is given; one left ``None`` is kept.

        Nothing picked at all removes the row.
        """
        given = {"foundation": foundation, "builder": builder, "light": light}

        def write(session: Any) -> None:
            pk = (current_workspace_id(), owner, PREFERENCE_KEY)
            row = session.get(SqlPreference, pk)
            try:
                old = _picks_from(json.loads(row.value) if row is not None else {})
            except ValueError:
                old = ModelPicks()
            merged = {
                category: given[category]
                if given[category] is not None
                else getattr(old, category)
                for category in CATEGORIES
            }
            if not any(merged.values()):
                if row is not None:
                    session.delete(row)
                return
            value = json.dumps(merged)
            if row is None:
                session.add(SqlPreference(user_id=owner, key=PREFERENCE_KEY, value=value))
            else:
                row.value = value

        run_write_transaction(self._session_immediate, "upsert_model_defaults", write)


def _deployment_upstreams(request: Request) -> set[str] | None:
    """The upstreams the deployment's ``connection: model`` providers are narrowed to (their
    ``upstreams`` lists), or ``None`` when any of them may use every upstream."""
    deployment = getattr(request.app.state, "sandbox_config", None)
    target = deployment.for_provider(None) if deployment is not None else None
    providers = ((target.host_config or {}).get("providers") or {}) if target is not None else {}
    lists = [
        entry.get("upstreams")
        for entry in providers.values()
        if isinstance(entry, dict) and entry.get("connection") == "model"
    ]
    if not lists or not all(isinstance(only, list) for only in lists):
        return None
    return {name for only in lists for name in only}


class ModelPreferencesBody(BaseModel):
    """Body of ``PUT /v1/me/model-preferences``: each map given replaces the stored one; an
    omitted map is kept. ``defaults`` is Foundation's earlier name, accepted for one release."""

    model_config = ConfigDict(extra="forbid")

    foundation: dict[str, str] | None = None
    builder: dict[str, str] | None = None
    light: dict[str, str] | None = None
    defaults: dict[str, str] | None = None


def foundation_resolver(store: ModelPreferenceStore) -> Callable[..., Awaitable[str | None]]:
    """``app.state.foundation_model``: a person's Foundation on a harness, resolved against a
    catalog when given (their pick when served, else automatic), else their raw pick."""
    from omnigent.superchat.models.tiers import person_models_from_catalog

    async def resolve(
        user_id: str | None, harness: str, catalog: dict[str, Any] | None
    ) -> str | None:
        picks = await asyncio.to_thread(store.read, user_id or RESERVED_USER_LOCAL)
        mine = picks.for_harness(harness)
        if catalog is None:
            return mine["foundation"]
        resolved = await asyncio.to_thread(person_models_from_catalog, catalog, mine)
        return resolved.foundation

    return resolve


def create_model_selection_router(
    store: ModelPreferenceStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: Any = None,
    conversation_store: Any = None,
) -> APIRouter:
    """``/me/models``, ``/me/model-preferences``, ``/me/model-providers`` and
    ``/sessions/{id}/helper-models`` (mount with ``prefix="/v1"``)."""
    from omnigent.server.inference_catalog import catalog_row_details
    from omnigent.server.routes.sandbox_inference import (
        configured_snapshot,
        preview_inference,
        validate_saved_selection,
    )
    from omnigent.superchat.models.tiers import person_models_from_catalog, tier_models
    from omnigent.superchat.models.upstreams import offered_providers

    router = APIRouter()

    @router.get("/me/model-providers")
    async def list_providers(request: Request) -> dict[str, Any]:
        """The providers a key can be saved for here, best first (the engine's registry, kept
        to the upstreams the deployment's model connections may use)."""
        require_user(request, auth_provider)
        return {"object": "list", "data": offered_providers(_deployment_upstreams(request))}

    @router.get("/sessions/{session_id}/helper-models")
    async def helper_models(request: Request, session_id: str, harness: str) -> dict[str, Any]:
        """``{"foundation", "builder", "light": id | null}``: the session owner's picks on
        *harness*, for a Helper dispatched from this session (``null``: no usable catalog, the
        Helper inherits the session's model)."""
        owner = None
        if conversation_store is not None:
            await require_access_and_level(
                get_user_id(request, auth_provider),
                session_id,
                LEVEL_READ,
                permission_store,
                conversation_store,
            )
            owner_of = getattr(conversation_store, "get_session_owner", None)
            owner = await asyncio.to_thread(owner_of, session_id) if owner_of else None
        return await tier_models(request.app.state, owner, normalize_inference_harness(harness))

    @router.get("/me/models")
    async def list_models(
        request: Request, harness: str, sandbox_provider: str | None = None
    ) -> dict[str, Any]:
        """The caller's catalog for *harness*, each row with the categories it resolves to.

        ``foundation_model``/``builder_model``/``light_model`` are what each category resolves
        to, ``picks`` the caller's own (``null``: automatic) and ``automatic`` what automatic
        means right now.
        """
        user_id = require_user(request, auth_provider)
        harness = normalize_inference_harness(harness)
        snapshot, catalog = await preview_inference(request, sandbox_provider, harness, user_id)
        picks = await asyncio.to_thread(store.read, user_id or RESERVED_USER_LOCAL)
        resolved = await asyncio.to_thread(
            person_models_from_catalog, catalog, picks.for_harness(harness)
        )
        rows = [
            {
                "id": row["id"],
                "label": row.get("displayName") or row["id"],
                **(catalog_row_details(snapshot, row["id"]) if snapshot is not None else {}),
                "is_default": bool(row.get("isDefault")),
                "categories": [c for c in CATEGORIES if resolved.model(c) == row["id"]],
            }
            for row in catalog["models"]
        ]
        response: dict[str, Any] = {
            "harness": harness,
            "status": catalog["status"],
            "provider_label": catalog["provider_label"],
            "default_model": catalog["default_model"],
            **{f"{c}_model": resolved.model(c) for c in CATEGORIES},
            "picks": dict(resolved.picks),
            "automatic": dict(resolved.automatic),
            "models": rows,
        }
        if catalog.get("error"):
            response["error"] = catalog["error"]
        return response

    def _picks_body(picks: ModelPicks) -> dict[str, Any]:
        # ``defaults`` mirrors Foundation for clients of the earlier shape, for one release.
        return {**picks.as_body(), "defaults": picks.foundation}

    @router.get("/me/model-preferences")
    async def get_preferences(request: Request) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        return _picks_body(await asyncio.to_thread(store.read, user_id or RESERVED_USER_LOCAL))

    async def _validated(request: Request, user_id: str | None, raw: dict[str, str]) -> dict:
        """*raw* keyed by normalized harness, each model checked against the caller's catalog.

        ``MODEL_NOT_SUPPORTED`` when the caller's connection does not serve the model;
        ``INVALID_INPUT`` when the harness has no usable catalog for the caller.
        """
        picked: dict[str, str] = {}
        for raw_harness, model in raw.items():
            harness = normalize_inference_harness(raw_harness)
            _, catalog = await preview_inference(request, None, harness, user_id)
            if catalog["status"] != "ready":
                raise OmnigentError(
                    catalog.get("error") or f"No models are available for harness {harness!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            if model not in {row["id"] for row in catalog["models"]}:
                raise OmnigentError(
                    f"Model {model!r} is not served by your connection for {harness!r}",
                    code=ErrorCode.MODEL_NOT_SUPPORTED,
                )
            picked[harness] = model
        return picked

    async def _follow_foundation(
        request: Request, user_id: str | None, harnesses: set[str]
    ) -> None:
        """Re-resolve the caller's Super Chat after their Foundation changed on *harnesses*:
        a configured session stores the new Foundation, any other drops its pin."""
        if conversation_store is None or not harnesses:
            return
        from omnigent.superchat.muse import find_super_chat, muse_key

        tenant = auth_provider.get_tenant(request) if auth_provider is not None else None
        key = muse_key(user_id or RESERVED_USER_LOCAL, tenant)
        conv = await asyncio.to_thread(find_super_chat, conversation_store, user_id, key)
        if conv is None:
            return
        snapshot = conv.inference_snapshot
        if configured_snapshot(snapshot):
            if normalize_inference_harness(snapshot["harness"]) not in harnesses:
                return
            try:
                model = await validate_saved_selection(
                    request, conv, None, foundation=True, user_id=user_id
                )
            except OmnigentError as exc:
                _logger.warning("Super Chat %s keeps its model: %s", conv.id, exc.message)
                return
        elif conv.model_override is None:
            return
        else:
            model = None
        if model == conv.model_override:
            return
        await asyncio.to_thread(
            conversation_store.update_conversation,
            conv.id,
            model_override=model,
            _unset_model_override=model is None,
        )
        _logger.info(
            "Super Chat %s follows the Foundation pick: %s -> %s",
            conv.id,
            conv.model_override,
            model or "unset",
        )

    @router.put("/me/model-preferences")
    async def put_preferences(request: Request, body: ModelPreferencesBody) -> dict[str, Any]:
        """Set any of the Foundation, Builder and Light picks (a map left out is kept)."""
        user_id = require_user(request, auth_provider)
        owner = user_id or RESERVED_USER_LOCAL
        current = await asyncio.to_thread(store.read, owner)
        given = {
            "foundation": body.foundation if body.foundation is not None else body.defaults,
            "builder": body.builder,
            "light": body.light,
        }
        maps = {
            category: None if raw is None else await _validated(request, user_id, raw)
            for category, raw in given.items()
        }
        await asyncio.to_thread(store.set, owner, **maps)
        saved = await asyncio.to_thread(store.read, owner)
        changed = {
            harness
            for harness in set(current.foundation) | set(saved.foundation)
            if current.foundation.get(harness) != saved.foundation.get(harness)
        }
        await _follow_foundation(request, user_id, changed)
        return _picks_body(saved)

    return router
