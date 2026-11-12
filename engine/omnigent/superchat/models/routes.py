"""Model connection routes: bring-your-own provider keys, per user or per organization.

``/v1/me/model-connections`` is the caller's own; ``/v1/admin/model-connections`` is the
organization's (admin only). Each has ``GET`` (list of
masked metadata ``{provider, hint, status, validated_at, label, scope}``), ``PUT /{provider}``
(body ``{api_key, label?}``: probe with the provider, seal, store, replacing that provider's
previous key) and ``DELETE /{provider}``. The key is never echoed, returned or logged: the body
model keeps it out of ``repr`` and carries no validators (so a validation error cannot quote it),
and errors name only the provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_admin, require_user
from omnigent.superchat.models.embeddings import (
    EmbeddingSetting,
    EmbeddingSettingStore,
    parse_setting,
)
from omnigent.superchat.models.probe import KeyRejectedError, ProbeUnavailableError, probe_key
from omnigent.superchat.models.store import (
    ORG_OWNER,
    SCOPE_ORG,
    SCOPE_USER,
    ConnectionInputError,
    ConnectionMeta,
    ModelConnectionStore,
)
from omnigent.superchat.models.upstreams import PROVIDERS, UPSTREAMS
from omnigent.superchat.sealing import VaultUnavailableError


class PutModelConnection(BaseModel):
    """Body of ``PUT .../model-connections/{provider}``."""

    model_config = ConfigDict(extra="forbid")

    # Defaults, not required fields: a "missing field" 422 would quote the whole body.
    api_key: str = Field("", repr=False)
    label: str | None = None


class PutEmbeddingModel(BaseModel):
    """Body of ``PUT /admin/embedding-model``: ``null`` for either field resets it."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    dimensions: int | None = None


def meta_to_response(meta: ConnectionMeta) -> dict[str, Any]:
    """Masked metadata only: never the key."""
    return {
        "provider": meta.provider,
        "hint": meta.hint,
        "status": meta.status,
        "validated_at": meta.validated_at,
        "label": meta.label,
        "scope": meta.scope,
    }


def create_model_connection_router(
    store: ModelConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
    embedding_settings: EmbeddingSettingStore | None = None,
) -> APIRouter:
    """Build the router, mounted with ``prefix="/v1"``. *transport* makes the probe injectable."""
    router = APIRouter()

    async def _me(request: Request) -> tuple[str, str]:
        return SCOPE_USER, require_user(request, auth_provider) or RESERVED_USER_LOCAL

    async def _org(request: Request) -> tuple[str, str]:
        await require_admin(
            request,
            auth_provider,
            permission_store,
            message="Admin privileges required to manage the organization's model connections",
        )
        return SCOPE_ORG, ORG_OWNER

    def _mount(prefix: str, owner: Callable[[Request], Awaitable[tuple[str, str]]]) -> None:
        @router.get(prefix)
        async def list_connections(request: Request) -> dict[str, Any]:
            """The owner's connections as masked metadata."""
            scope, owner_id = await owner(request)
            metas = await asyncio.to_thread(store.list, scope, owner_id)
            return {"object": "list", "data": [meta_to_response(m) for m in metas]}

        @router.put(prefix + "/{provider}")
        async def put_connection(
            request: Request, provider: str, body: PutModelConnection
        ) -> dict[str, Any]:
            """Probe the key with its provider, then store it sealed (replacing the old one)."""
            scope, owner_id = await owner(request)
            offered = [name for name in PROVIDERS if UPSTREAMS[name].configured]
            if provider not in offered:
                raise OmnigentError(
                    f"provider must be one of: {', '.join(offered)}",
                    code=ErrorCode.INVALID_INPUT,
                )
            api_key = body.api_key.strip()
            if not api_key:
                raise OmnigentError("api_key is required", code=ErrorCode.INVALID_INPUT)
            try:
                await probe_key(provider, api_key, transport=transport)
            except KeyRejectedError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from None
            except ProbeUnavailableError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.MODEL_PROVIDER_UNREACHABLE) from None
            try:
                meta = await asyncio.to_thread(
                    lambda: store.put(scope, owner_id, provider, api_key, label=body.label)
                )
            except VaultUnavailableError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.SUPERCHAT_NOT_CONFIGURED) from None
            except ConnectionInputError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from None
            return meta_to_response(meta)

        @router.delete(prefix + "/{provider}", status_code=204)
        async def delete_connection(request: Request, provider: str) -> Response:
            """Remove the owner's key for that provider."""
            scope, owner_id = await owner(request)
            if not await asyncio.to_thread(store.delete, scope, owner_id, provider):
                raise OmnigentError("No model connection saved", code=ErrorCode.NOT_FOUND)
            return Response(status_code=204)

    _mount("/me/model-connections", _me)
    _mount("/admin/model-connections", _org)

    if embedding_settings is not None:

        def _setting_view(setting: EmbeddingSetting) -> dict[str, Any]:
            return {"model": setting.model, "dimensions": setting.dimensions}

        @router.get("/admin/embedding-model")
        async def get_embedding_model(request: Request) -> dict[str, Any]:
            """The organization's embedding model (the default until an admin sets one)."""
            await _org(request)
            return _setting_view(await asyncio.to_thread(embedding_settings.get))

        @router.put("/admin/embedding-model")
        async def put_embedding_model(request: Request, body: PutEmbeddingModel) -> dict[str, Any]:
            """Choose the embedding model files are indexed with; a null model resets it."""
            await _org(request)
            if body.model is None:
                await asyncio.to_thread(embedding_settings.set, None)
            else:
                setting = parse_setting({"model": body.model, "dimensions": body.dimensions})
                if body.dimensions is not None and setting.dimensions != body.dimensions:
                    raise OmnigentError(
                        "dimensions must be between 8 and 8192", code=ErrorCode.INVALID_INPUT
                    )
                await asyncio.to_thread(embedding_settings.set, setting)
            return _setting_view(await asyncio.to_thread(embedding_settings.get))

    return router
