"""``GET /v1/sessions/{id}/tasks``: what a turn and ``deck_new`` read (:meth:`TaskService.view`).

It changes nothing; the runner reports no task events any more.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.server.auth import LEVEL_READ
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.server.routes._errors import session_not_found
from omnigent.superchat.tasks.service import TaskService


def create_tasks_router(
    service: TaskService, *, permission_store: Any = None, auth_provider: Any = None
) -> APIRouter:
    """Build the tasks router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    async def _check(request: Request, session_id: str, level: int) -> None:
        if permission_store is not None:
            await require_access(
                get_user_id(request, auth_provider),
                session_id,
                level,
                permission_store,
                service.conversations,
            )

    @router.get("/sessions/{session_id}/tasks")
    async def read_tasks(request: Request, session_id: str) -> dict[str, Any]:
        """The person's latest message, the session's Round, its card answer and the Rounds
        still working."""
        await _check(request, session_id, LEVEL_READ)
        view = await asyncio.to_thread(service.view, session_id)
        if view is None:
            raise session_not_found()
        return view

    return router
