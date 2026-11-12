"""Session host status route: whether the host is ready, and how its launch is going."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def register_host_status_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the host status route on router."""

    @router.get(
        "/sessions/{session_id}/host",
        include_in_schema=False,
        response_model=None,
    )
    async def get_host_status(request: Request, session_id: str) -> dict[str, Any]:
        """
        Report whether the session's host is ready and how its launch is going.

        :param session_id: Session identifier, e.g. ``"conv_abc123"``.
        :returns: ``{"ready": bool, "launch": ...}``; ``ready`` is whether the Conversation's
            runner is connected (Files and tools can reach it). ``launch`` is
            ``{"stage": ..., "error": ...}`` while the managed-sandbox launch is in flight or has
            failed (the session snapshot's ``sandbox_status``), else ``null``.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        from omnigent.server.routes.sessions import _session_sandbox_status_cache

        cached = _session_sandbox_status_cache.get(session_id)
        launch = {"stage": cached.stage, "error": cached.error} if cached is not None else None
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        tunnels = getattr(request.app.state, "tunnel_registry", None)
        ready = bool(
            conv is not None
            and conv.runner_id
            and tunnels is not None
            and tunnels.get(conv.runner_id) is not None
        )
        return {"ready": ready, "launch": launch}
