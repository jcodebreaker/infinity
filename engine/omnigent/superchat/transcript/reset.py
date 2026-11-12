"""``POST /sessions/{id}/reset``: clear a Super Chat's conversation.

A reset is a rollover whose checkpoint has no summary (:func:`~omnigent.context.rollover.
build_reset_item`): the Muse's next turn cold-starts from the reset framing alone, so it forgets
the chat history. Nothing else is touched: memory, files, Helpers and Goals stay, and
the earlier items stay stored (the ``session_history`` tool and ``GET .../items`` still read them).

The transcript (``GET .../transcript``) then starts at the reset; ``before_reset=true`` pages
the history before it. Refused while a turn is running (its remaining output would land after
the reset and belong to the cleared context).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Request

from omnigent.context.labels import is_superside_chat
from omnigent.context.rollover import (
    _MID_TURN_LIVE_STATUSES,
    RESET_RESPONSE_PREFIX,
    build_reset_item,
)
from omnigent.entities import ConversationItem, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

#: Compaction items scanned (newest first) for the latest reset.
_RESET_SCAN_PAGE = 100
_RESET_SCAN_PAGES = 5


def latest_reset_item(conv_store: ConversationStore, session_id: str) -> ConversationItem | None:
    """The session's latest reset checkpoint, or ``None`` (scans its compaction items only)."""
    after: str | None = None
    for _ in range(_RESET_SCAN_PAGES):
        page = conv_store.list_items(
            session_id, limit=_RESET_SCAN_PAGE, after=after, order="desc", type="compaction"
        )
        for item in page.data:
            if item.response_id.startswith(RESET_RESPONSE_PREFIX):
                return item
        if not page.has_more or not page.data:
            return None
        after = page.last_id
    return None


def register_reset_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register ``POST /sessions/{session_id}/reset``."""

    @router.post("/sessions/{session_id}/reset", response_model=None)
    async def reset_session(request: Request, session_id: str) -> dict[str, Any]:
        """
        Clear a Super Chat: the Muse starts a fresh context, everything else stays.

        :param session_id: The Super Chat (the Conversation; not a Helper).
        :returns: ``{"session_id", "reset_item_id", "created_at"}``.
        :raises OmnigentError: 403 unless the caller owns it or it is not a Super Chat;
            404 if it does not exist; 409 while a turn is running.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        conv = access.conversation or await asyncio.to_thread(
            conversation_store.get_conversation, session_id
        )
        if conv is None:
            raise _session_not_found()
        if conv.kind == "sub_agent" or not is_superside_chat(conv.labels):
            raise OmnigentError(
                "Only a Super Chat's Conversation can be reset", code=ErrorCode.FORBIDDEN
            )
        if conv.live_status in _MID_TURN_LIVE_STATUSES:
            raise OmnigentError("A turn is running; reset after it ends", code=ErrorCode.CONFLICT)

        newest = await asyncio.to_thread(
            conversation_store.list_items, session_id, limit=1, order="desc"
        )
        if not newest.data:
            return {"session_id": session_id, "reset_item_id": None, "created_at": None}
        if newest.data[0].response_id.startswith(RESET_RESPONSE_PREFIX):
            # Already cleared and nothing since: idempotent.
            item = newest.data[0]
            return {
                "session_id": session_id,
                "reset_item_id": item.id,
                "created_at": item.created_at,
            }

        checkpoint = build_reset_item(newest.data[0].id, model=conv.model_override)
        [stored] = await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [
                NewConversationItem(
                    type="compaction",
                    response_id=f"{RESET_RESPONSE_PREFIX}{uuid.uuid4().hex}",
                    data=checkpoint,
                    created_by=user_id,
                )
            ],
        )
        await _after_reset(request, conversation_store, session_id, stored.id)
        return {
            "session_id": session_id,
            "reset_item_id": stored.id,
            "created_at": stored.created_at,
        }


async def _after_reset(
    request: Request, conversation_store: ConversationStore, session_id: str, item_id: str
) -> None:
    """Tell the runner to drop its warm context, settle the Rounds no turn will now answer,
    learn from the cleared chat, signal clients."""
    from omnigent.server.routes._sessions.common import get_server_runner_router
    from omnigent.server.routes._sessions.helpers import _forward_session_change_to_runner
    from omnigent.superchat.family.signals import notify_chat_reset
    from omnigent.superchat.rounds.record import reset_rounds

    # Best-effort: with no runner reachable, the next spawn loads history from the server.
    await _forward_session_change_to_runner(
        session_id, get_server_runner_router(), {"type": "context_reset"}
    )
    await reset_rounds(session_id)
    coordinator = getattr(request.app.state, "memory_upkeep_coordinator", None)
    if coordinator is not None:
        owner = await asyncio.to_thread(conversation_store.get_session_owner, session_id)
        if owner:
            coordinator.schedule(owner)
    notify_chat_reset(session_id, item_id)
