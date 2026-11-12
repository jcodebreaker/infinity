"""``/v1/me/outbox``: the person's proactive messages, as the ``web`` channel reads them.

* ``GET /me/outbox?after=<item_id>&limit=`` -> ``{"data": [row]}``, oldest first: the messages
  after ``after`` (the newest ``limit`` when it is absent). A row is ``{item_id,
  conversation_id, kind, topic_key, urgency, title, line, created_at}``.
* ``GET /me/outbox/unread`` -> ``{count, first_item_id, last_seen_item_id}``: the messages after
  the last one the person saw.
* ``POST /me/outbox/seen`` ``{item_id}`` -> the same: the person saw the chat up to *item_id*
  (never moves back).
* ``POST /me/outbox/{item_id}/not-useful`` ``{reason?}`` -> ``{"ok": true}``: the stored rule
  the Lookout turns into a mute.

Every string is redacted of the secrets the engine knows (:mod:`omnigent.server.redaction`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.redaction import request_redactor
from omnigent.server.routes._auth_helpers import require_user
from omnigent.superchat.outbox.store import WEB_CHANNEL, SqlAlchemyOutboxStore, owner_key


class SeenBody(BaseModel):
    """Body of ``POST /v1/me/outbox/seen``."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64)


class NotUsefulBody(BaseModel):
    """Body of ``POST /v1/me/outbox/{item_id}/not-useful``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


def _item_id(value: str | None) -> str | None:
    """*value* when it is a well-formed item id, else ``None`` (an unknown message)."""
    if not value:
        return None
    try:
        uuid_to_bytes(value)
    except InvalidUuidError:
        return None
    return value


def _not_found() -> OmnigentError:
    return OmnigentError("Message not found", code=ErrorCode.NOT_FOUND)


def create_outbox_router(
    store: SqlAlchemyOutboxStore, *, auth_provider: AuthProvider | None
) -> APIRouter:
    """Build the outbox router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    @router.get("/me/outbox")
    async def list_outbox(
        request: Request,
        after: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """The caller's proactive messages after *after*, oldest first."""
        user_id = require_user(request, auth_provider)
        rows = await asyncio.to_thread(
            store.list_after, owner_key(user_id), _item_id(after), limit=limit
        )
        return request_redactor(request, user_id).deep({"data": [r.to_wire() for r in rows]})

    @router.get("/me/outbox/unread")
    async def unread(request: Request) -> dict[str, Any]:
        """How many proactive messages the caller has not seen, and the first of them."""
        owner = owner_key(require_user(request, auth_provider))
        return await asyncio.to_thread(store.unread, owner)

    @router.post("/me/outbox/seen")
    async def seen(request: Request, body: SeenBody) -> dict[str, Any]:
        """The caller saw the Conversation up to the proactive message *item_id*."""
        owner = owner_key(require_user(request, auth_provider))
        item_id = _item_id(body.item_id)
        if item_id is None or not await asyncio.to_thread(
            store.advance, WEB_CHANNEL, owner, item_id
        ):
            raise _not_found()
        return await asyncio.to_thread(store.unread, owner)

    @router.post("/me/outbox/{item_id}/not-useful")
    async def not_useful(request: Request, item_id: str, body: NotUsefulBody) -> dict[str, bool]:
        """The caller marks a proactive message *Not useful*."""
        owner = owner_key(require_user(request, auth_provider))
        valid = _item_id(item_id)
        if valid is None or (
            await asyncio.to_thread(store.not_useful, owner, valid, body.reason) is None
        ):
            raise _not_found()
        return {"ok": True}

    return router
