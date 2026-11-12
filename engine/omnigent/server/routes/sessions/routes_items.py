"""Items and child-session routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import (
    APIRouter,
    Query,
    Request,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from omnigent.context.labels import is_artifact_chat
from omnigent.entities import CompactionData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    background_session_titles_enabled,
)
from omnigent.server.permissions import check_session_access
from omnigent.server.redaction import session_redactor
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import (
    STALE_CURSOR_RESPONSE,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import _format_sse
from omnigent.server.routes._sessions.orchestration import (
    _child_session_summaries_from_conversations,
)
from omnigent.server.schemas import (
    ChildSessionList,
    PaginatedList,
    SessionContextSummaryResponse,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.superchat.activity.derive import (
    activities_missing_titles,
    activity_to_dict,
    get_activity,
    list_activities,
    resolve_super_chat_id,
    schedule_missing_titles,
)
from omnigent.superchat.family.changes import watch_activity_changes


async def _caller_readable_ids(
    user_id: str | None,
    candidate_ids: list[str],
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> set[str]:
    """Which of *candidate_ids* the caller may READ.

    A Super Chat family / related-chats listing is resolved by *owner*
    (``list_chat_family``, ``list_related_chats``), not by the caller's own
    grants, so it can hand back sessions the caller has no access to at all.
    This re-runs
    the same per-session check the routes already require on the URL
    session, against every family member, before any of it reaches the
    caller. The owner always passes (their own grant satisfies READ); a
    collaborator shared only a slice of the family sees only that slice.

    :returns: *candidate_ids* unfiltered when *permission_store* is
        ``None`` (permission enforcement disabled, same posture every other
        route takes).
    """
    if permission_store is None:
        return set(candidate_ids)

    def _check_all() -> set[str]:
        return {
            candidate_id
            for candidate_id in candidate_ids
            if check_session_access(
                user_id, candidate_id, LEVEL_READ, permission_store, conversation_store
            )
        }

    return await asyncio.to_thread(_check_all)


class MarkReadRequest(BaseModel):
    """Body of ``POST /v1/sessions/{id}/read``."""

    model_config = ConfigDict(extra="forbid")

    #: Read up to this item (its ``created_at``); omitted means "everything, now".
    item_id: str | None = None


def register_items_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
) -> None:
    """Register the items routes on router."""

    @router.get(
        "/sessions/{session_id}/items",
        response_model=None,
        responses={200: {"model": PaginatedList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_session_items(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
        type: str | None = Query(default=None, pattern="^compaction$"),
    ) -> PaginatedList:
        """
        List items in a session with cursor-based pagination.

        Delegates to the conversation items store — session_id is
        the conversation_id. Same pagination contract as
        ``GET /v1/conversations/{id}/items``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return
            (1-1000, default 100).
        :param after: Cursor — return items after this item ID,
            e.g. ``"msg_abc123"``.
        :param before: Cursor — return items before this item ID.
        :param order: Sort order, ``"asc"`` (chronological,
            default) or ``"desc"``.
        :param type: Only items of this type. ``"compaction"`` with
            ``order=desc&limit=1`` is the session's latest checkpoint.
        :returns: A :class:`PaginatedList` of conversation items.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=limit,
            after=after,
            before=before,
            order=order,
            type=type,
        )
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        data = [redactor.deep(m.to_api_dict()) for m in page.data]
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/items/search ──────────────────
    # Session-scoped full-text search over conversation items — the REST
    # counterpart to ``ConversationStore.search(query, conversation_id=...)``
    # for callers (the runner's native-relay tool dispatch) with no
    # in-process store. Mirrors ``list_session_items`` above, scoped by
    # the same access check.

    @router.get(
        "/sessions/{session_id}/items/search",
        response_model=None,
        responses={200: {"model": PaginatedList}},
    )
    async def search_session_items(
        request: Request,
        session_id: str,
        query: str = Query(min_length=1),
        limit: int = Query(default=10, ge=1, le=20),
        scope: str = Query(default="session", pattern="^(session|family)$"),
    ) -> PaginatedList:
        """
        Full-text search over one session's own conversation items, or its family's messages.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param query: The search query string.
        :param limit: Maximum number of results (1-20, default 10).
        :param scope: ``session`` (default): this session's items, ranked by relevance.
            ``family``: the messages of its Conversation (no Helpers), newest first, each ``{"session_id", "message_id", "role",
            "created_at", "text", "item"}``; only chats the caller may read.
        :returns: A :class:`PaginatedList` of matching item dicts
            (``first_id``/``last_id``/``has_more`` unset — search results are not
            cursor-paginated).
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        if scope == "family":
            from omnigent.superchat.family.search import family_chats, search_family

            chats = await asyncio.to_thread(family_chats, conversation_store, session_id)
            allowed_ids = await _caller_readable_ids(
                user_id, [chat.id for chat in chats], permission_store, conversation_store
            )
            hits = await asyncio.to_thread(
                search_family,
                conversation_store,
                [chat for chat in chats if chat.id in allowed_ids],
                query,
                limit=limit,
            )
            return PaginatedList(data=[redactor.deep(hit) for hit in hits])
        items = await asyncio.to_thread(
            conversation_store.search,
            query,
            conversation_id=session_id,
            limit=limit,
        )
        return PaginatedList(data=[redactor.deep(m.to_api_dict()) for m in items])

    # ── GET /sessions/{session_id}/related_chats ──────────────────
    # The chats session_id may read besides itself, for ``session_history``'s
    # ``list_chats`` action run by the runner: a scheduled or ad-hoc Helper's
    # parent Conversation. ``list_related_chats`` resolves by owner, not by the
    # caller's grants, so ``_caller_readable_ids`` re-checks each one against
    # the caller's own permissions before it is returned.

    @router.get(
        "/sessions/{session_id}/related_chats",
        response_model=None,
        responses={200: {"model": PaginatedList}},
    )
    async def list_related_chats_route(
        request: Request,
        session_id: str,
    ) -> PaginatedList:
        """
        List the chats one session may read besides itself.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: A :class:`PaginatedList` of chat summary dicts
            (``id``, ``title``, ``created_at``, ``updated_at``,
            ``last_message_preview``, ``archived``, ``live``, ``project``);
            not cursor-paginated.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        from omnigent.context.rollover import list_related_chats

        chats = await asyncio.to_thread(list_related_chats, conversation_store, session_id)
        allowed_ids = await _caller_readable_ids(
            user_id, [chat["id"] for chat in chats], permission_store, conversation_store
        )
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        return PaginatedList(
            data=[redactor.deep(chat) for chat in chats if chat["id"] in allowed_ids]
        )

    # ── POST /sessions/{session_id}/read ──────────────────────────
    # Server-side "the caller read this chat": moves the caller's read-state
    # baseline (the one the session list's ``unread`` uses).

    @router.post("/sessions/{session_id}/read", response_model=None)
    async def mark_session_read(
        request: Request,
        session_id: str,
        body: MarkReadRequest | None = None,
    ) -> dict[str, Any]:
        """
        Mark a chat read for the caller, up to ``item_id`` or up to now.

        :param session_id: Any session the caller can READ.
        :param body: Optional ``{"item_id"}``; the baseline never moves back.
        :returns: ``{"session_id", "unread": false, "last_read_at"}``.
        :raises OmnigentError: 403 without READ; 404 if the session or ``item_id`` is unknown.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        from omnigent.superchat.family.signals import notify_session_changed
        from omnigent.superchat.family.unread import mark_read

        read_at = await asyncio.to_thread(
            mark_read,
            conversation_store,
            user_id,
            session_id,
            body.item_id if body is not None else None,
        )
        if read_at is None:
            raise OmnigentError("Item not found", code=ErrorCode.NOT_FOUND)
        await notify_session_changed(conversation_store, session_id)
        return {"session_id": session_id, "unread": False, "last_read_at": read_at}

    # ── GET /sessions/{session_id}/context_summary ────────────────
    # "Knows our conversation" hover (docs/architecture.md): the
    # session's own latest rollover checkpoint, if it has rolled over.

    @router.get(
        "/sessions/{session_id}/context_summary",
        response_model=SessionContextSummaryResponse,
    )
    async def get_context_summary(
        request: Request,
        session_id: str,
    ) -> SessionContextSummaryResponse:
        """
        The session's latest rollover checkpoint summary, or ``None``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: ``{"summary", "summary_body", "created_at"}``, all ``None`` when the
            session has never rolled over.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=1,
            order="desc",
            type="compaction",
        )
        from omnigent.context.rollover import RESET_RESPONSE_PREFIX, person_facing_summary

        if (
            not page.data
            or not isinstance(page.data[0].data, CompactionData)
            or page.data[0].response_id.startswith(RESET_RESPONSE_PREFIX)
        ):
            # Never rolled over, or cleared since: nothing is carried over.
            return SessionContextSummaryResponse(summary=None, created_at=None)
        item = page.data[0]
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        return SessionContextSummaryResponse(
            summary=redactor.text(item.data.summary),
            summary_body=redactor.deep(person_facing_summary(item.data.summary)),
            created_at=item.created_at,
        )

    # ── GET /sessions/{session_id}/child_sessions ────────────────

    @router.get(
        "/sessions/{session_id}/child_sessions",
        response_model=None,
        responses={200: {"model": ChildSessionList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_child_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        tool: str | None = Query(default=None),
        session_name: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List sub-agent (child) sessions under a parent session.

        Returns a page of :class:`ChildSessionSummary` objects
        derived from child conversations (``kind="sub_agent"``,
        ``parent_conversation_id=session_id``) plus each child's
        latest task. Powers the web / REPL debug surfaces' "child
        sessions" panel without parsing parent
        ``function_call_output`` JSON handles. Pagination contract
        matches :func:`list_session_items` so existing client code
        can reuse the same cursor logic.

        :param request: Inbound HTTP request; carries the caller
            identity used to authorize READ on the parent session.
        :param session_id: Parent session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of children to return
            (1-1000, default 20 — sub-agent fan-out is typically
            sparse compared to conversation items).
        :param after: Cursor — return children whose id appears
            after this one in sort order,
            e.g. ``"conv_child123"``.
        :param before: Cursor — return children before this one.
        :param order: Sort direction, ``"desc"`` (newest-first,
            default) or ``"asc"``. Sort column is ``created_at``.
        :param tool: When set, only return children whose title
            starts with this agent type (the segment before the
            ``":"``). Combined with ``session_name`` to form the
            exact title ``"{tool}:{session_name}"`` for server-side
            filtering.
        :param session_name: When set alongside ``tool``, only
            return children whose title matches
            ``"{tool}:{session_name}"`` exactly.
        :returns: A :class:`PaginatedList` of
            :class:`ChildSessionSummary` objects.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        # Require READ on the parent before listing its children (no cross-user enumeration).
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation
        if parent is None:
            parent = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if parent is None:
            raise _session_not_found()
        title_filter: str | None = None
        if tool and session_name:
            title_filter = f"{tool}:{session_name}"
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            kind="sub_agent",
            parent_conversation_id=session_id,
            order=order,
            sort_by="created_at",
            title=title_filter,
        )
        data = await _child_session_summaries_from_conversations(
            # A file's studio (ADR 0024) is not one of the chat's Helpers.
            [child for child in page.data if not is_artifact_chat(child.labels)],
            session_id,
            conversation_store,
        )
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        return PaginatedList(
            data=[redactor.deep(child.model_dump(mode="json")) for child in data],
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/activities ────────────────────
    # The Activity Feed: every Activity under session_id's Super Chat family
    # (the Super Chat itself and its Sub-agents). Derived on read from
    # conversations/items — see omnigent/superchat/activity/derive.py.

    @router.get(
        "/sessions/{session_id}/activities",
        response_model=None,
        responses={200: {"model": PaginatedList}},
    )
    async def list_session_activities(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        before: int | None = Query(default=None),
        tz: str | None = Query(default=None, max_length=64),
    ) -> PaginatedList:
        """
        List the Activity Feed for ``session_id``'s Super Chat family.

        :param session_id: A Super Chat session id.
        :param limit: Maximum Activities to return, newest-first (1-100).
        :param before: Only Activities started strictly before this epoch
            timestamp.
        :returns: A :class:`PaginatedList` of Activity summary dicts (no
            per-step detail — see ``GET .../activities/{activity_id}``),
            each carrying a ``date`` field for day-grouping. Empty when
            ``session_id`` isn't a ``superside-chat`` session.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        activities = await asyncio.to_thread(
            list_activities,
            conversation_store,
            session_id,
            before=before,
            limit=limit,
            tz=tz,
        )
        allowed_ids = await _caller_readable_ids(
            user_id,
            list({activity.chat_id for activity in activities}),
            permission_store,
            conversation_store,
        )
        activities = [activity for activity in activities if activity.chat_id in allowed_ids]
        missing_titles = activities_missing_titles(activities)
        if (
            missing_titles
            and background_title_coordinator is not None
            and background_session_titles_enabled(request.headers)
        ):
            title_chats = await asyncio.to_thread(
                conversation_store.get_conversations,
                list({activity.chat_id for activity in missing_titles}),
            )
            schedule_missing_titles(
                conversation_store, background_title_coordinator, missing_titles, title_chats
            )
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        data = await asyncio.to_thread(
            lambda: [redactor.deep(activity_to_dict(activity, tz)) for activity in activities]
        )
        return PaginatedList(data=data)

    # ── GET /sessions/{session_id}/activities/stream ─────────────
    # Live "the feed changed" signal for the same family (no content: the
    # client re-reads the feed). Registered before ``/activities/{activity_id}``
    # so "stream" is never taken for an id.

    @router.get("/sessions/{session_id}/activities/stream", response_model=None)
    async def stream_session_activity_changes(
        request: Request,
        session_id: str,
    ) -> StreamingResponse:
        """
        Subscribe to ``activities.changed`` signals for ``session_id``'s family.

        :param session_id: A Super Chat session id.
        :returns: An SSE stream of ``activities.changed`` / heartbeat frames.
        :raises OmnigentError: 403 if the caller lacks READ on ``session_id``;
            404 if no session exists there or it is not a Super Chat family.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if await asyncio.to_thread(resolve_super_chat_id, conversation_store, session_id) is None:
            raise _session_not_found()

        async def frames():
            async for event in watch_activity_changes(conversation_store, session_id):
                if await request.is_disconnected():
                    break
                yield _format_sse(event["type"], event)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── GET /sessions/{session_id}/activities/{activity_id} ──────
    # One Activity in full: steps carry their capped call/result detail.

    @router.get(
        "/sessions/{session_id}/activities/{activity_id}",
        response_model=None,
    )
    async def get_session_activity(
        request: Request,
        session_id: str,
        activity_id: str,
        tz: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        """
        Return one Activity with full step detail.

        :param session_id: A Super Chat session id.
        :param activity_id: An id previously returned by
            ``GET .../activities``.
        :returns: The Activity dict, steps including each call's
            arguments/output (capped).
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there, the session
            isn't a ``superside-chat`` session, or ``activity_id`` doesn't
            resolve within its family.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        activity = await asyncio.to_thread(
            get_activity,
            conversation_store,
            session_id,
            activity_id,
            tz,
        )
        if activity is None:
            raise _session_not_found()
        allowed_ids = await _caller_readable_ids(
            user_id, [activity.chat_id], permission_store, conversation_store
        )
        if activity.chat_id not in allowed_ids:
            raise _session_not_found()
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        return redactor.deep(activity_to_dict(activity, tz))
