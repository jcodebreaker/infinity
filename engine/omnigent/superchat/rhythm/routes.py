"""``/v1/me/rhythm``: behaviour events from any client, the plain summary line and *Start over*.

* ``POST /me/rhythm/events`` ``{item_id, event}`` with ``event`` one of ``shown``, ``opened``,
  ``acted_on`` -> ``{"ok": true, "recorded": bool}`` (``recorded`` is ``false`` for a repeat).
  404 when *item_id* is not one of the caller's proactive messages.
* ``GET /me/rhythm`` -> ``{line, learning}``: what was learned, in one plain line.
* ``POST /me/rhythm/reset`` -> ``{line, learning}``: forget what was learned (never the rules).

Reach rules (what the person said, ADR 0038):

* ``GET /me/reach-rules`` -> ``{rules: [{id, kind, params, said, until, line}]}``.
* ``POST /me/reach-rules`` ``{session_id, action, ...}``: what the ``reach_rules`` tool calls;
  only the person's own Conversation may write (never a Helper or a scheduled run).
  ``add`` ``{kind, params, said, expires_at?}`` -> ``{rule, replaced}``; ``remove``
  ``{id?, kind?}`` -> ``{removed}``.
* ``DELETE /me/reach-rules/{id}`` -> ``{removed}``: Settings' remove.

*Not useful* is the outbox's mute and never reaches these routes (ADR 0038).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.superchat.feature import is_super_chat
from omnigent.superchat.rhythm.reach import MAX_SAID
from omnigent.superchat.rhythm.service import Rhythm


class EventBody(BaseModel):
    """Body of ``POST /v1/me/rhythm/events``."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64)
    event: Literal["shown", "opened", "acted_on"]


class RuleBody(BaseModel):
    """Body of ``POST /v1/me/reach-rules``."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=64)
    action: Literal["add", "remove"]
    kind: str | None = Field(default=None, max_length=16)
    params: dict[str, Any] = Field(default_factory=dict)
    said: str = Field(default="", max_length=MAX_SAID * 2)
    expires_at: str | None = Field(default=None, max_length=32)
    id: str | None = Field(default=None, max_length=32)


def create_rhythm_router(
    rhythm: Rhythm,
    *,
    auth_provider: AuthProvider | None,
    conversation_store: Any = None,
    permission_store: Any = None,
) -> APIRouter:
    """Build the rhythm router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    def _owner(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    @router.post("/me/rhythm/events")
    async def report(request: Request, body: EventBody) -> dict[str, Any]:
        """A client saw, opened or acted on one of the caller's proactive messages."""
        owner = _owner(request)
        try:
            uuid_to_bytes(body.item_id)
        except InvalidUuidError as exc:
            raise OmnigentError("Message not found", code=ErrorCode.NOT_FOUND) from exc
        found = await asyncio.to_thread(rhythm.is_proactive, owner, body.item_id)
        if not found:
            raise OmnigentError("Message not found", code=ErrorCode.NOT_FOUND)
        recorded = await asyncio.to_thread(
            rhythm.record, owner, body.item_id, body.event, datetime.now(UTC)
        )
        return {"ok": True, "recorded": recorded}

    @router.get("/me/rhythm")
    async def read(request: Request) -> dict[str, Any]:
        """What the assistant learned about when the caller likes to hear from it."""
        return await asyncio.to_thread(rhythm.summary, _owner(request))

    @router.post("/me/rhythm/reset")
    async def reset(request: Request) -> dict[str, Any]:
        """*Start over*: forget what was learned."""
        owner = _owner(request)
        await asyncio.to_thread(rhythm.reset, owner)
        return await asyncio.to_thread(rhythm.summary, owner)

    async def _require_conversation(owner: str, session_id: str) -> None:
        """Only the person's own Conversation writes a rule: never a Helper, a hidden scheduled
        run (the Lookout's, the brief's) or another person's chat."""
        try:
            uuid_to_bytes(session_id)
        except InvalidUuidError as exc:
            raise OmnigentError("invalid session id", code=ErrorCode.INVALID_INPUT) from exc
        if conversation_store is None:
            raise OmnigentError("no conversations here", code=ErrorCode.INVALID_INPUT)
        await require_access(owner, session_id, LEVEL_OWNER, permission_store, conversation_store)
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if (
            conv is None
            or conv.parent_conversation_id is not None
            or not is_super_chat(conv.labels)
        ):
            raise OmnigentError(
                "only the person's own Conversation sets reach rules",
                code=ErrorCode.INVALID_INPUT,
            )

    @router.get("/me/reach-rules")
    async def list_rules(request: Request) -> dict[str, Any]:
        """The caller's reach rules in force."""
        return {"rules": await asyncio.to_thread(rhythm.rules, _owner(request))}

    @router.post("/me/reach-rules")
    async def write_rule(request: Request, body: RuleBody) -> dict[str, Any]:
        """Add or lift a rule the person said in their Conversation."""
        owner = _owner(request)
        await _require_conversation(owner, body.session_id)
        if body.action == "remove":
            if not body.id and not body.kind:
                raise OmnigentError("pass the rule's id or a kind", code=ErrorCode.INVALID_INPUT)
            removed = await asyncio.to_thread(
                rhythm.remove_rules, owner, rule_id=body.id, kind=body.kind
            )
            return {"removed": removed}
        if body.kind is None:
            raise OmnigentError("pass the rule's kind", code=ErrorCode.INVALID_INPUT)
        result = await asyncio.to_thread(
            rhythm.add_rule, owner, body.kind, body.params, body.said, body.expires_at
        )
        if isinstance(result, str):
            raise OmnigentError(result, code=ErrorCode.INVALID_INPUT)
        return result

    @router.delete("/me/reach-rules/{rule_id}")
    async def delete_rule(request: Request, rule_id: str) -> dict[str, Any]:
        """Settings' remove."""
        owner = _owner(request)
        removed = await asyncio.to_thread(rhythm.remove_rules, owner, rule_id=rule_id)
        if not removed:
            raise OmnigentError("Rule not found", code=ErrorCode.NOT_FOUND)
        return {"removed": removed}

    return router
