"""Managed-sandbox model previews and saved session-policy validation."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from omnigent.context.labels import SUBAGENT_LABEL_KEY, is_artifact_chat, is_superside_chat
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inference_config import (
    binding_for_harness,
    normalize_inference_harness,
    parse_inference_config,
    resolve_bound_provider,
)
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access_and_level, require_user


def inference_service(request: Request) -> Any:
    """Use the service owned by this server's application state."""
    return _state_inference_service(request.app.state)


def _state_inference_service(app_state: Any) -> Any:
    from omnigent.server.inference_catalog import SandboxInferenceService

    service = getattr(app_state, "inference_catalog", None)
    if service is None:
        service = SandboxInferenceService(app_state)
        app_state.inference_catalog = service
    return service


def actual_harness(spec: Any, override: str | None = None) -> str:
    """Resolve the harness identity used to select a profile binding."""
    declared = spec.executor.config.get("harness") or spec.executor.type
    return normalize_inference_harness(override or declared)


def configured_snapshot(snapshot: dict[str, Any] | None) -> bool:
    """Whether the session's own harness has an inference binding."""
    return (
        snapshot is not None
        and binding_for_harness(snapshot["runtime_config"], snapshot["harness"]) is not None
    )


def managed_inference_configured(request: Request, provider: str | None) -> bool:
    """Whether a managed target requires an identifiable harness before creation."""
    deployment = getattr(request.app.state, "sandbox_config", None)
    target = deployment.for_provider(provider) if deployment is not None else None
    return bool(target and parse_inference_config(target.host_config or {}))


def selected_catalog_model(
    catalog: dict[str, Any], model: str | None, preferred: str | None = None
) -> str:
    """Reject unavailable selections before persistence or prompt dispatch.

    Order: the explicit *model*, then the caller's *preferred* default when this catalog serves
    it, then the catalog's own default.
    """
    if catalog.get("status") != "ready":
        raise OmnigentError(
            catalog.get("error") or "No available models match this harness's configuration",
            # Discovery that could not reach the provider is an outage to retry (503); an empty,
            # misconfigured or unconfigured list is the request's to fix.
            code=ErrorCode.RUNNER_UNAVAILABLE
            if catalog.get("retryable")
            else ErrorCode.INVALID_INPUT,
        )
    if model is None and preferred in {row["id"] for row in catalog["models"]}:
        model = preferred
    selected = model or catalog.get("default_model")
    if not selected or selected not in {row["id"] for row in catalog["models"]}:
        raise OmnigentError(
            f"Model {selected!r} is not available in this session's configured model list",
            code=ErrorCode.INVALID_INPUT,
        )
    return selected


async def prepare_create_inference(
    request: Request,
    body: Any,
    spec: Any,
    user_id: str | None,
    conversation_store: Any,
    *,
    harness_override: str | None = None,
    model_override: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Capture the accepted configuration before a session or sandbox exists."""
    harness = actual_harness(spec, harness_override)
    if (
        harness == "auto"
        and body.host_type == "managed"
        and managed_inference_configured(request, body.sandbox_provider)
    ):
        raise OmnigentError(
            "Choose a harness to use this sandbox's configured inference providers",
            code=ErrorCode.INVALID_INPUT,
        )
    snapshot = None
    parent = None
    if body.parent_session_id:
        parent = await asyncio.to_thread(
            conversation_store.get_conversation, body.parent_session_id
        )
        if parent is not None and parent.inference_snapshot is not None:
            snapshot = copy.deepcopy(parent.inference_snapshot)
            snapshot["harness"] = harness
    if snapshot is None and body.host_type == "managed":
        snapshot = await inference_service(request).prepare(
            body.sandbox_provider, harness, user_id, agent_auth=spec.executor.auth
        )
        revision = body.inference_configuration_revision
        if revision is not None and (
            snapshot is None or revision != snapshot["configuration_revision"]
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "inference_configuration_changed",
                    "message": "Harness configuration changed. Refresh the model choices.",
                },
            )
    if snapshot is not None and harness == "auto":
        raise OmnigentError(
            "Choose a harness to use the parent session's saved inference providers",
            code=ErrorCode.INVALID_INPUT,
        )
    if not configured_snapshot(snapshot):
        return snapshot, model_override
    assert snapshot is not None
    resolve_bound_provider(snapshot["runtime_config"], harness, spec.executor.auth)
    if spec.executor.profile:
        raise OmnigentError(
            "The agent's Databricks profile conflicts with the configured harness provider",
            code=ErrorCode.INVALID_INPUT,
        )
    if getattr(body, "cost_control_mode_override", None) == "on" or harness_override == "auto":
        raise OmnigentError(
            "Automatic provider routing is unavailable for a bound inference profile",
            code=ErrorCode.INVALID_INPUT,
        )
    catalog = await inference_service(request).catalog(snapshot)
    snapshot["catalog"] = catalog
    preferred = None
    if parent is not None and parent.inference_snapshot is not None:
        # A sub-agent pins its own model (a helper tier, a bundle's ``model:``) only where the
        # owner's connection serves it; otherwise it inherits the parent's model.
        served = {row["id"] for row in catalog.get("models", [])}
        if model_override is not None and model_override not in served:
            model_override = parent.model_override if parent.model_override in served else None
    elif follows_person_picks(getattr(body, "labels", None)):
        preferred = await user_default_model(request, user_id, harness, catalog)
    selected = selected_catalog_model(catalog, model_override, preferred)
    return snapshot, selected


def snapshot_serves_model(snapshot: dict[str, Any] | None, model: str) -> bool:
    """Whether a session's saved catalog serves *model*; a session without one serves anything."""
    if not configured_snapshot(snapshot):
        return True
    assert snapshot is not None
    catalog = snapshot.get("catalog")
    if not isinstance(catalog, dict) or catalog.get("status") != "ready":
        return True
    return model in {row["id"] for row in catalog["models"]}


def follows_person_picks(labels: Mapping[str, str] | None) -> bool:
    """Whether a session runs on its owner's Foundation pick: a Super Chat itself, never a
    Helper or a file's studio chat. Every other session starts on the connection's default."""
    return (
        is_superside_chat(labels)
        and SUBAGENT_LABEL_KEY not in (labels or {})
        and not is_artifact_chat(labels)
    )


async def user_default_model(
    request: Request, user_id: str | None, harness: str, catalog: dict[str, Any] | None = None
) -> str | None:
    """The caller's Foundation model for *harness*: resolved against *catalog* (their pick when
    it serves it, else the automatic choice), or their raw pick when no catalog is given.

    Answered by ``app.state.foundation_model`` (the Super Chat models feature installs it);
    ``None`` without it.
    """
    resolve = getattr(request.app.state, "foundation_model", None)
    if resolve is None:
        return None
    return await resolve(user_id, normalize_inference_harness(harness), catalog)


async def session_model(
    app_state: Any, conversation: Conversation, conversation_store: Any
) -> str | None:
    """The model *conversation* runs: its stored pin, else, for a session that follows its
    owner's picks (:func:`follows_person_picks`), the owner's Foundation on its harness (resolved
    against its saved catalog, else the catalog a new session would get). ``None``: the harness
    runs its connection's default.

    The one answer for both the turn's dispatch and the model the web shows.
    """
    if conversation.model_override is not None:
        return conversation.model_override
    resolve = getattr(app_state, "foundation_model", None)
    if resolve is None or not follows_person_picks(conversation.labels):
        return None
    service = _state_inference_service(app_state)
    snapshot = conversation.inference_snapshot
    if configured_snapshot(snapshot):
        assert snapshot is not None
        owner, harness = snapshot.get("owner_id"), snapshot["harness"]
        catalog = await service.catalog(snapshot)
    else:
        from omnigent.server.routes._sessions.helpers import _resolve_harness

        harness = await asyncio.to_thread(_resolve_harness, conversation)
        if harness is None:
            return None
        owner = await asyncio.to_thread(conversation_store.get_session_owner, conversation.id)
        try:
            fresh = await service.prepare(None, normalize_inference_harness(harness), owner)
        except (OmnigentError, ValueError):
            return None  # no key or no reachable catalog: the connection's default runs
        catalog = (fresh or {}).get("catalog") or {}
    if catalog.get("status") != "ready":
        return None
    return await resolve(owner, normalize_inference_harness(harness), catalog)


async def preview_inference(
    request: Request,
    provider: str | None,
    harness: str,
    user_id: str | None,
    auth: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The snapshot a new session would capture and its model catalog, never raising for config.

    Shared by the model-options preview and ``GET /v1/me/models``.
    """
    try:
        snapshot = await inference_service(request).prepare(
            provider, harness, user_id, agent_auth=auth
        )
        if snapshot is not None:
            return snapshot, snapshot["catalog"]
    except (OmnigentError, ValueError) as exc:
        return None, {
            "configured": True,
            "models": [],
            "configuration_revision": None,
            "provider_label": None,
            "default_model": None,
            "status": "unavailable",
            "error": exc.message
            if isinstance(exc, OmnigentError)
            else "Invalid inference configuration",
        }
    return None, {
        "configured": False,
        "models": [],
        "configuration_revision": None,
        "provider_label": None,
        "default_model": None,
        "status": "unconfigured",
    }


async def validate_saved_selection(
    request: Request,
    conversation: Conversation,
    model: str | None,
    *,
    foundation: bool = False,
    user_id: str | None = None,
) -> str | None:
    """Recheck availability using saved provider/credential identity.

    With no *model*: *user_id*'s Foundation on the session's harness when *foundation* is set and
    the session follows its owner's picks (:func:`follows_person_picks`), resolved against the
    saved catalog; else the catalog's own default.
    """
    snapshot = conversation.inference_snapshot
    if not configured_snapshot(snapshot):
        return model
    assert snapshot is not None
    catalog = await inference_service(request).catalog(snapshot)
    preferred = None
    if (
        model is None
        and foundation
        and follows_person_picks(conversation.labels)
        and catalog.get("status") == "ready"
    ):
        preferred = await user_default_model(request, user_id, snapshot["harness"], catalog)
    return selected_catalog_model(catalog, model, preferred)


def create_sandbox_inference_router(
    *,
    agent_store: Any,
    agent_cache: Any,
    conversation_store: Any,
    permission_store: Any,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    """Expose authenticated previews without provisioning a host."""
    router = APIRouter()

    @router.get("/sandbox-providers/{provider}/harnesses/{harness}/model-options")
    async def model_options(
        request: Request, provider: str, harness: str, agent_id: str | None = None
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        auth = None
        if agent_id is not None:
            agent = await asyncio.to_thread(agent_store.get, agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="Agent not found")
            if agent.session_id:
                await require_access_and_level(
                    user_id, agent.session_id, LEVEL_READ, permission_store, conversation_store
                )
            spec = (
                await asyncio.to_thread(
                    agent_cache.load,
                    agent.id,
                    agent.bundle_location,
                    expand_env=agent.session_id is None,
                )
            ).spec
            harness = actual_harness(spec, harness)
            auth = spec.executor.auth
        return (await preview_inference(request, provider, harness, user_id, auth))[1]

    return router
