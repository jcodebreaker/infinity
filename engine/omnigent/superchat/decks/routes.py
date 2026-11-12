"""``POST /v1/decks/{artifact_id}/export`` (Export buttons) and ``GET /v1/decks/themes`` (the
theme dictionary, and each theme's sample deck for its preview).

Wakes the person's host the way a message does, then runs the ``deck_export`` tool on its
runner (``/mcp/execute``) so the Chromium that renders the deck is the one in their host.
The result is a new artifact in the deck's chat, shown as a file the person delivered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import ResourceEventData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.stores import ConversationStore
from omnigent.superchat.artifacts import (
    SqlAlchemyArtifactStore,
    load_artifact,
    request_owner,
)
from omnigent.superchat.decks import kit
from omnigent.superchat.decks.names import is_deck_name
from omnigent.superchat.family.signals import notify_message_done
from omnigent.superchat.transcript.blocks import DELIVERED_ARTIFACT_RESOURCE, DELIVERED_EVENT

logger = logging.getLogger(__name__)

_TOOL = "deck_export"

#: How long the export waits for the host to wake and its runner to connect.
WAKE_TIMEOUT_S = 120.0
#: The helper's own limit is 180 s; leave room for the upload.
EXECUTE_TIMEOUT_S = 220.0


class DeckExportBody(BaseModel):
    """``POST /decks/{id}/export`` body."""

    format: Literal["pptx", "pdf"]


def create_decks_router(
    store: SqlAlchemyArtifactStore,
    *,
    conversation_store: ConversationStore,
    runner_router: Any = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the decks router, mounted with ``prefix="/v1"`` after the artifacts router."""
    router = APIRouter()

    async def _wake(request: Request, session_id: str) -> Any:
        from omnigent.server.routes._sessions.common import get_server_runner_router
        from omnigent.server.routes._sessions.orchestration import ensure_runner_connected

        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        unavailable = OmnigentError(
            "Your machine is still starting; try again shortly",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
        try:
            client, _ = await asyncio.wait_for(
                ensure_runner_connected(
                    session_id=session_id,
                    conv=conv,
                    app_state=request.app.state,
                    conversation_store=conversation_store,
                    runner_router=runner_router or get_server_runner_router(),
                ),
                timeout=WAKE_TIMEOUT_S,
            )
        except TimeoutError:
            raise unavailable from None
        if client is None:
            raise unavailable
        return client

    @router.get("/decks/themes")
    async def deck_themes(request: Request) -> dict[str, Any]:
        """The theme dictionary with thumbnails, for the deck panel's Theme picker."""
        request_owner(request, auth_provider)
        return {"themes": kit.theme_gallery(), "default": kit.DEFAULT_THEME}

    @router.get("/decks/themes/{theme_id}/sample")
    async def deck_theme_sample(request: Request, theme_id: str) -> dict[str, Any]:
        """The kit's sample deck built in one theme (``.deck.html``), for a theme preview."""
        request_owner(request, auth_provider)
        if theme_id not in kit.templates():
            raise OmnigentError("Unknown theme", code=ErrorCode.NOT_FOUND)
        return {"html": await asyncio.to_thread(kit.sample_deck, theme_id)}

    @router.get("/decks/{artifact_id}/theme")
    async def deck_theme(request: Request, artifact_id: str) -> dict[str, Any]:
        """The theme id a deck is on (``null`` when hand-made), for the picker's check mark."""
        item = await load_artifact(store, request_owner(request, auth_provider), artifact_id)
        if not is_deck_name(item.name):
            raise OmnigentError("That file isn't a deck", code=ErrorCode.INVALID_INPUT)
        data = await asyncio.to_thread(store.read, item)
        return {"theme": kit.deck_theme_id(data.decode("utf-8", "replace"))}

    @router.post("/decks/{artifact_id}/export")
    async def export_deck(
        request: Request, artifact_id: str, body: DeckExportBody
    ) -> dict[str, Any]:
        """Export a deck to ``pptx`` / ``pdf`` in the person's host as a new artifact."""
        item = await load_artifact(store, request_owner(request, auth_provider), artifact_id)
        if not is_deck_name(item.name):
            raise OmnigentError("That file isn't a deck", code=ErrorCode.INVALID_INPUT)
        session_id = item.parent_session_id
        client = await _wake(request, session_id)
        try:
            resp = await client.post(
                f"/v1/sessions/{session_id}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {
                        "name": _TOOL,
                        "arguments": {"artifact_id": item.id, "format": body.format},
                    },
                },
                timeout=EXECUTE_TIMEOUT_S,
            )
            payload = resp.json()
        except Exception as exc:
            raise OmnigentError(
                f"The export couldn't reach your machine: {exc}",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        failure = _failure(payload)
        if failure is not None:
            raise OmnigentError(failure, code=ErrorCode.INVALID_INPUT)
        try:
            result = json.loads(payload["result"]["output"])
        except (KeyError, TypeError, ValueError):
            result = None
        if not isinstance(result, dict):
            raise OmnigentError("The export didn't finish", code=ErrorCode.INVALID_INPUT)
        if result.get("error"):
            raise OmnigentError(str(result["error"])[:300], code=ErrorCode.INVALID_INPUT)
        await _show_in_chat(conversation_store, session_id, result)
        result.pop("type", None)
        return result

    return router


async def _show_in_chat(
    conversation_store: ConversationStore,
    session_id: str,
    result: dict[str, Any],
) -> None:
    """Record the exported file in the deck's chat as a file the person delivered.

    A ``resource_event`` item (``resource_type="artifact"``), not a tool call: the Muse never
    made this call, so its history must not hold one. The transcript projects it as a file card
    marked as the person's own action, and the agent loop skips it (``NON_CONTENT_ITEM_TYPES``).
    Best effort: the file is already saved and in the Library, so a failure here never fails the
    export.
    """
    try:
        stored = await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [
                NewConversationItem(
                    type="resource_event",
                    response_id=f"deck-export-{uuid.uuid4().hex}",
                    data=ResourceEventData(
                        event_type=DELIVERED_EVENT,
                        resource_id=str(result.get("id") or ""),
                        resource_type=DELIVERED_ARTIFACT_RESOURCE,
                        resource=dict(result),
                    ),
                )
            ],
        )
    except Exception:
        logger.warning(
            "deck_export: could not show the file in chat %s", session_id, exc_info=True
        )
        return
    notify_message_done(session_id, stored[-1].id)


def _failure(payload: Any) -> str | None:
    """The runner's JSON-RPC error message, or ``None`` when the call went through."""
    if not isinstance(payload, dict):
        return "The export didn't finish"
    err = payload.get("error")
    if err:
        message = err.get("message") if isinstance(err, dict) else err
        return str(message or "The export didn't finish")[:300]
    return None


__all__ = ["DeckExportBody", "create_decks_router"]
