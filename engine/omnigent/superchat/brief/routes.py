"""``GET /v1/me/brief/latest``: the caller's newest morning brief, for any client's Home.

The first call makes the person's brief task (ADR 0037: created on first use), so a client
needs no setup step. *Refresh now* is the scheduled task's own run route
(``POST /v1/scheduled-tasks/{task_id}/run``) with the ``task_id`` answered here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.evals.identity import is_eval_identity
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.superchat.artifacts.routes import request_owner
from omnigent.superchat.brief.service import BriefService
from omnigent.superchat.muse import find_super_chat, muse_key


def create_brief_router(
    service: BriefService,
    *,
    conversation_store: Any,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the brief router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    @router.get("/me/brief/latest")
    async def latest_brief(request: Request) -> dict[str, Any]:
        """The newest brief page and the task that makes it.

        :returns: ``{"task_id", "enabled", "next_run_at", "brief"}``; ``brief`` is ``None``
            before the first run (``{artifact_id, group_id, title, version, created_at, line,
            home}`` otherwise, ``home`` the Home lines), and every field is empty for a person
            without a Conversation yet.
        """
        user_id = require_user(request, auth_provider)
        tenant = auth_provider.get_tenant(request) if auth_provider is not None else None
        key = muse_key(user_id or RESERVED_USER_LOCAL, tenant)
        conv = await asyncio.to_thread(find_super_chat, conversation_store, user_id, key)
        if conv is None or conv.agent_id is None or is_eval_identity(user_id):
            return {"task_id": None, "enabled": False, "next_run_at": None, "brief": None}
        owner = None if (user_id or RESERVED_USER_LOCAL) == RESERVED_USER_LOCAL else user_id
        task, changed = await asyncio.to_thread(service.ensure, conv, owner)
        scheduler = getattr(request.app.state, "scheduled_task_scheduler", None)
        if scheduler is not None and changed:
            scheduler.update(task)
        brief = await asyncio.to_thread(
            service.latest, task, request_owner(request, auth_provider)
        )
        return {
            "task_id": task.id,
            "enabled": task.state == "active",
            "next_run_at": scheduler.next_run_at(task.id) if scheduler is not None else None,
            "brief": brief,
        }

    return router
