"""``GET /sessions/{id}/rounds`` and ``GET /sessions/{id}/rounds/{round_id}``: a Conversation's
Rounds, each read as the fold of its events. A Helper's session reads its Conversation's."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.schemas import RoundList, RoundSummary, RoundView
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.superchat.rounds.fold import fold, summary
from omnigent.superchat.rounds.store import SqlAlchemyRoundStore
from omnigent.superchat.rounds.ux_log import shown

#: Most Rounds one list returns.
MAX_LIMIT = 50


def register_round_routes(
    router: APIRouter,
    *,
    store: SqlAlchemyRoundStore,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the two Round reads on *router* (mounted with ``prefix="/v1"``)."""

    async def conversation_of(request: Request, session_id: str) -> str:
        access = await _require_access_and_level(
            _get_user_id(request, auth_provider),
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        session = access.conversation or await asyncio.to_thread(
            conversation_store.get_conversation, session_id
        )
        if session is None:
            raise _session_not_found()
        return session.root_conversation_id or session.id

    @router.get("/sessions/{session_id}/rounds", response_model=RoundList)
    async def list_rounds(
        request: Request, session_id: str, limit: int = Query(20, ge=1, le=MAX_LIMIT)
    ) -> RoundList:
        """
        The Conversation's latest Rounds, newest first, each with its status.

        :raises OmnigentError: 403 without READ on ``session_id``; 404 if it does not exist.
        """
        conversation_id = await conversation_of(request, session_id)

        def read() -> list[RoundSummary]:
            rows = []
            for round_id in store.recent(conversation_id, limit):
                view = fold(store.events(round_id, conversation_id), round_id)
                shown("list", conversation_id, round_id, view)
                rows.append(RoundSummary(round_id=round_id, **summary(view)))
            return rows

        return RoundList(data=await asyncio.to_thread(read))

    @router.get("/sessions/{session_id}/rounds/{round_id}", response_model=RoundView)
    async def get_round(request: Request, session_id: str, round_id: str) -> RoundView:
        """
        One Round of the Conversation, folded from its events.

        :raises OmnigentError: 403 without READ on ``session_id``; 404 if it does not exist or
            the Round is not one of its Conversation's.
        """
        conversation_id = await conversation_of(request, session_id)
        events = await asyncio.to_thread(store.events, round_id, conversation_id)
        if not events:
            raise OmnigentError("Round not found", code=ErrorCode.NOT_FOUND)
        view = fold(events, round_id)
        shown("view", conversation_id, round_id, view)
        view.pop("after_settle")
        return RoundView(round_id=round_id, **view)
