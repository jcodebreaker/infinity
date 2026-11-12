"""``/v1/me/vault``: the person's encrypted logins (secrets vault).

The value goes in once, from the client's secure form, and is never echoed: every response
carries metadata only, except ``/fill`` which the runner calls to type a value into the
host's browser and which returns the saved origin with the value so the page helper can
refuse any other site.
Owner-scoped.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores import ConversationStore
from omnigent.superchat.vault.store import (
    VaultEntry,
    VaultInputError,
    VaultRequest,
    VaultStore,
    VaultUnavailableError,
)


class SaveRequest(BaseModel):
    """Body of ``POST /v1/me/vault``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    site: str
    username: str = ""
    password: str = Field(repr=False)
    request_id: str | None = None


class OpenRequest(BaseModel):
    """Body of ``POST /v1/me/vault/requests``."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    name: str
    site: str
    reason: str = ""


class FillRequest(BaseModel):
    """Body of ``POST /v1/me/vault/fill``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    field: str
    session_id: str


def entry_to_response(entry: VaultEntry) -> dict[str, Any]:
    """Metadata only: never a value."""
    return {
        "id": entry.id,
        "name": entry.name,
        "site": entry.site,
        "username": entry.username,
        "created_at": entry.created_at,
        "last_used_at": entry.last_used_at,
    }


def request_to_response(req: VaultRequest) -> dict[str, Any]:
    """A secure-entry request as the client renders it."""
    return {
        "id": req.id,
        "session_id": req.session_id,
        "name": req.name,
        "site": req.site,
        "reason": req.reason,
        "status": req.status,
    }


def create_vault_router(
    store: VaultStore,
    *,
    conversation_store: ConversationStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the vault router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    def _owner(request: Request) -> str | None:
        user_id = require_user(request, auth_provider)
        owner = user_id if user_id is not None else RESERVED_USER_LOCAL
        return None if owner == RESERVED_USER_LOCAL else owner

    def _check_id(value: str, what: str) -> None:
        try:
            uuid_to_bytes(value)
        except InvalidUuidError as exc:
            raise OmnigentError(f"invalid {what}", code=ErrorCode.INVALID_INPUT) from exc

    async def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except VaultUnavailableError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.FORBIDDEN) from exc
        except VaultInputError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc

    @router.post("/me/vault")
    async def save_secret(request: Request, body: SaveRequest) -> dict[str, Any]:
        """Save a login from the secure form. The password is encrypted and never echoed."""
        owner = _owner(request)
        if body.request_id:
            _check_id(body.request_id, "request_id")
        entry = await _run(
            store.save,
            user_id=owner,
            name=body.name,
            site=body.site,
            username=body.username,
            password=body.password,
            request_id=body.request_id,
        )
        return entry_to_response(entry)

    @router.get("/me/vault")
    async def list_secrets(request: Request) -> dict[str, Any]:
        """The caller's saved logins: site, username, created and last used."""
        owner = _owner(request)
        entries = await _run(store.entries, user_id=owner)
        return {"entries": [entry_to_response(e) for e in entries]}

    @router.delete("/me/vault/{secret_id}", status_code=204)
    async def delete_secret(request: Request, secret_id: str) -> None:
        """Delete one saved login."""
        owner = _owner(request)
        _check_id(secret_id, "secret id")
        if not await _run(store.delete, secret_id, user_id=owner):
            raise OmnigentError("Secret not found", code=ErrorCode.NOT_FOUND)

    @router.post("/me/vault/requests")
    async def open_request(request: Request, body: OpenRequest) -> dict[str, Any]:
        """Open a one-time secure-entry request (the Muse's ``vault_request_secret``)."""
        owner = _owner(request)
        _check_id(body.session_id, "session_id")
        req = await _run(
            store.create_request,
            user_id=owner,
            session_id=body.session_id,
            name=body.name,
            site=body.site,
            reason=body.reason,
        )
        return request_to_response(req)

    @router.get("/me/vault/requests/{request_id}")
    async def get_request(request: Request, request_id: str) -> dict[str, Any]:
        """One request (for the secure-entry card)."""
        owner = _owner(request)
        _check_id(request_id, "request id")
        req = await _run(store.get_request, request_id, user_id=owner)
        if req is None:
            raise OmnigentError("Request not found", code=ErrorCode.NOT_FOUND)
        return request_to_response(req)

    @router.post("/me/vault/fill")
    async def fill(request: Request, body: FillRequest) -> dict[str, Any]:
        """Hand the runner one value to type; the browser helper enforces the saved origin.

        Only the runner bound to ``session_id`` may call this (its runner bearer plus binding
        token); the owner's own credentials, the client's API included, are refused and audited.
        """
        owner = _owner(request)
        _check_id(body.session_id, "session_id")
        identity = getattr(auth_provider, "runner_identity", lambda _r: None)(request)
        bound = None
        if identity is not None and conversation_store is not None:
            bound = conversation_store.get_runner_ids([body.session_id]).get(body.session_id)
        if identity is None or identity[0] != owner or bound is None or bound != identity[1]:
            await _run(store.refuse_fill, body.name, user_id=owner, session_id=body.session_id)
            raise OmnigentError(
                "Only the session's runner may fill from the vault", code=ErrorCode.FORBIDDEN
            )
        try:
            value, saved_site = await _run(
                store.reveal,
                body.name,
                user_id=owner,
                field=body.field,
                session_id=body.session_id,
            )
        except KeyError as exc:
            raise OmnigentError("No saved login with that name", code=ErrorCode.NOT_FOUND) from exc
        return {"value": value, "site": saved_site}

    return router
