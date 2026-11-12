"""``GET /sessions/{id}/transcript``: a session's items as typed chat blocks.

After a reset (``POST .../reset``) the transcript starts at the reset: paging back stops there
(``has_more`` false) and ``reset`` names the checkpoint. ``before_reset=true`` reads the
history before the latest reset instead, paged the same way. Every string is redacted of the
secrets the engine knows (:mod:`omnigent.server.redaction`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent.context.labels import is_artifact_chat
from omnigent.context.rollover import _MID_TURN_LIVE_STATUSES
from omnigent.entities import Conversation
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.redaction import session_redactor
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.superchat.artifacts.links import attach_links
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
from omnigent.superchat.family.tree import sub_agent_status
from omnigent.superchat.feature import TRANSCRIPT_FIELDS
from omnigent.superchat.lineage import session_lineage
from omnigent.superchat.rounds.read import (
    attach_round_files,
    attach_round_ids,
    item_rounds,
)
from omnigent.superchat.rounds.store import SqlAlchemyRoundStore
from omnigent.superchat.transcript.blocks import helper_session_ids, project_items
from omnigent.superchat.transcript.reset import latest_reset_item

#: Items a page asks for when one call is not enough to find a tool call's output.
_LOOKAHEAD_ITEMS = 100


#: How long a transcript read waits on the runner for its live queue before showing none: one
#: deadline for the whole lookup (finding the runner, connecting, reading), since every page of
#: a live Conversation waits on it, the first one the person sees included.
_RUNNER_QUEUE_READ_TIMEOUT_S = 0.5


async def _waiting_item_ids(request: Request, session_id: str) -> set[str]:
    """The persisted user messages waiting in the session runner's buffer (empty if unknown).

    Live state only: the runner owns its queue, so nothing about it is stored here.
    """
    from omnigent.server.routes._sessions.helpers import _get_runner_client

    async def read() -> Any:
        client = await _get_runner_client(
            session_id, getattr(request.app.state, "runner_router", None)
        )
        if client is None:
            return None
        resp = await client.get(f"/v1/sessions/{session_id}/buffered")
        return resp.json().get("item_ids") if resp.status_code == 200 else None

    try:
        ids = await asyncio.wait_for(read(), _RUNNER_QUEUE_READ_TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- the note is optional; the transcript is not
        return set()
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def read_transcript(
    conv_store: ConversationStore,
    conversation: Conversation,
    *,
    before: str | None,
    limit: int,
    before_reset: bool = False,
    rounds: SqlAlchemyRoundStore | None = None,
    artifacts: SqlAlchemyArtifactStore | None = None,
) -> dict[str, Any]:
    """One page of a session's transcript, oldest message first.

    :param rounds: The Round record: each message's ``round_id`` is read from it
        (:func:`attach_round_ids`) and a Conversation's file cards are drawn from it
        (:func:`attach_round_files`).
    :param artifacts: Where those cards' titles and sizes are read.

    Pages walk back from the newest items (``before`` is the ``older_cursor`` of the page
    after). Paging stops at the latest reset; ``before_reset`` pages the items older than it
    instead.
    """
    lineage = session_lineage(conv_store, conversation)
    reset = latest_reset_item(conv_store, conversation.id)
    reset_info = {"item_id": reset.id, "created_at": reset.created_at} if reset else None
    live = conversation.live_status in _MID_TURN_LIVE_STATUSES
    if before_reset and reset is None:
        return _page([], has_more=False, older_cursor=None, lineage=lineage, reset=None, live=live)
    newer = before  # the oldest item of the newer page; the reset item below is a cut, not a page
    if before_reset and before is None:
        before = reset.id if reset else None
    # Newest first, so "further along" the store's sort is older: the older page is `after`.
    page = conv_store.list_items(conversation.id, limit=limit, after=before, order="desc")
    newest_first = [item.to_api_dict() for item in page.data]
    has_more = page.has_more
    cut = reset.id if reset is not None and not before_reset else None
    index = next((i for i, item in enumerate(newest_first) if item["id"] == cut), -1)
    if index != -1:
        newest_first, has_more = newest_first[:index], False
    items = list(reversed(newest_first))
    # A call whose output is newer than this page ends it: read ahead for the output.
    if newest_first and before is not None:
        calls = {i["call_id"] for i in items if i["type"] == "function_call" and i.get("call_id")}
        done = {i["call_id"] for i in items if i["type"] == "function_call_output"}
        if calls - done:
            ahead = conv_store.list_items(
                conversation.id, limit=_LOOKAHEAD_ITEMS, after=newest_first[0]["id"], order="asc"
            )
            items += [
                i.to_api_dict()
                for i in ahead.data
                if i.type == "function_call_output" and i.data.call_id in calls - done
            ]
    helper_ids = helper_session_ids(items)
    statuses: dict[str, str] = {}
    if helper_ids:
        for helper_id, helper in conv_store.get_conversations(helper_ids).items():
            statuses[helper_id] = sub_agent_status(helper)
    # A file's studio shows its runtime lines (ADR 0024); every other chat hides them.
    messages = project_items(
        items,
        helper_statuses=statuses,
        keep_notices=is_artifact_chat(conversation.labels),
        conversation_id=conversation.id,
    )
    if rounds is not None:
        attach_round_ids(rounds, conv_store, conversation, items, messages)
    if rounds is not None and conversation.kind != "sub_agent":
        newer_round = item_rounds(rounds, conv_store, conversation.id, [newer]) if newer else []
        attach_round_files(
            rounds,
            conversation.id,
            messages,
            newer_round_id=newer_round[0] if newer_round else None,
            artifacts=artifacts,
        )
    return _page(
        messages,
        has_more=has_more,
        older_cursor=newest_first[-1]["id"] if newest_first and has_more else None,
        lineage=lineage,
        reset=reset_info,
        live=live,
    )


def _page(
    data: list[dict[str, Any]],
    *,
    has_more: bool,
    older_cursor: str | None,
    lineage: dict[str, Any],
    reset: dict[str, Any] | None,
    live: bool,
) -> dict[str, Any]:
    return {
        "data": data,
        "has_more": has_more,
        "older_cursor": older_cursor,
        "lineage": lineage,
        "reset": reset,
        "live": live,
    }


def register_transcript_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    artifact_store: SqlAlchemyArtifactStore | None = None,
    round_store: SqlAlchemyRoundStore | None = None,
) -> None:
    """Register ``GET /sessions/{session_id}/transcript``.

    :param artifact_store: Where the person's messages' artifact links are read from; without
        it no message carries ``artifact``.
    :param round_store: The Round record file cards may be drawn from (:func:`read_transcript`).
    """

    @router.get("/sessions/{session_id}/transcript", response_model=None)
    async def get_session_transcript(
        request: Request,
        session_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        before_reset: bool = Query(default=False),
    ) -> dict[str, Any]:
        """
        A session's transcript as typed blocks (``text``, ``card``, ``helper``, ``file``,
        ``secure_entry``, ``error``).

        :param session_id: Any session the caller can READ (same gate as ``.../items``).
        :param limit: Items per page, newest page first.
        :param before: Cursor: the ``older_cursor`` of the page after this one.
        :param before_reset: Page the history before the latest reset instead (empty when the
            session was never reset).
        :returns: ``{"data": [message], "has_more", "older_cursor", "lineage", "reset", "live"}``:
            ``reset`` is ``{"item_id", "created_at"}`` of the latest reset or ``None``; ``live``
            is whether a turn is in flight. A user message still waiting behind the running
            turn has ``delivered: "queued"`` (from the runner's live queue). A person's message
            linked to an artifact has ``artifact`` (``{artifact_id, name, version, how}``, or
            ``None`` when the person said it is about none). Every message has ``round_id``: the
            Round it is in (the id of the person's message that opened it), ``None`` outside
            any Round. A proactive message (a scheduled run's turn wrote it, ADR 0036) has
            ``proactive``: ``{kind, topic_key?, urgency}``.
        :raises OmnigentError: 403 without READ; 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conversation = access.conversation or await asyncio.to_thread(
            conversation_store.get_conversation, session_id
        )
        if conversation is None:
            raise _session_not_found()
        page = await asyncio.to_thread(
            read_transcript,
            conversation_store,
            conversation,
            before=before,
            limit=limit,
            before_reset=before_reset,
            rounds=round_store,
            artifacts=artifact_store,
        )
        if artifact_store is not None:
            await asyncio.to_thread(attach_links, artifact_store, conversation.id, page["data"])
        for add_fields in list(TRANSCRIPT_FIELDS.values()):
            await asyncio.to_thread(add_fields, conversation.id, page["data"])
        if page["live"]:
            # A message waiting behind the running turn: the runner's live queue says which.
            waiting = await _waiting_item_ids(request, conversation.id)
            for message in page["data"]:
                if message["role"] == "user" and message["id"] in waiting:
                    message["delivered"] = "queued"
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        return redactor.deep(page)
