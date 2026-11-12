"""``GET /sessions/{root}/family/stream``: the family's events as SSE."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.helpers import _format_sse
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.superchat.family.stream import watch_family
from omnigent.superchat.family.tree import resolve_super_chat_id


def register_family_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register ``GET /sessions/{session_id}/family/stream``."""

    @router.get("/sessions/{session_id}/family/stream", response_model=None)
    async def stream_family(request: Request, session_id: str) -> StreamingResponse:
        """
        Subscribe to the events of a Super Chat and its Helpers.

        :param session_id: The Super Chat.
        :returns: SSE frames: ``message.done``, ``message.delivery``, ``turn.done``,
            ``chat.reset``, ``chats.changed``, ``activities.changed``, ``artifacts.changed``,
            ``round.updated``, ``chat.status``, ``asks.changed``, ``session.heartbeat``.
            Ids only; the client refetches.
        :raises OmnigentError: 403 without READ on ``session_id``; 404 if it is not a
            Super Chat family.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        root_id = await asyncio.to_thread(resolve_super_chat_id, conversation_store, session_id)
        if root_id is None:
            raise _session_not_found()

        async def frames():
            async for event in watch_family(conversation_store, root_id):
                if await request.is_disconnected():
                    break
                yield _format_sse(event["type"], event)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
