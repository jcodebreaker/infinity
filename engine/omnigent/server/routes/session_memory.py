"""Routes for long-term memory (``docs/context.md``).

All endpoints live under ``/v1/sessions/{session_id}/memory``: reads require
``LEVEL_READ`` on the session, writes (``remember``/``forget``) require
``LEVEL_EDIT``. The user memory is scoped to is **always** the session's
owner (``conversation_store.get_session_owner``), never a value from the
request body or query string — the same rule the ``memory_*`` built-in tools
apply when called in-process
(``omnigent.superchat.memory_tools.tools.resolve_memory_user``), so native-relay
dispatch (which reaches this API over REST) and in-process dispatch agree on
whose memory is being read or written. The caller must also BE that owner:
a session shared with another collaborator never exposes the owner's Memory
through it, even to a collaborator with EDIT on the session itself.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.memory.service import MemoryService
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.server.routes._errors import session_not_found
from omnigent.server.schemas import (
    MemoryEditRequest,
    MemoryForgetRequest,
    MemoryRememberRequest,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_SEARCH_DEFAULT_LIMIT = 10
_SEARCH_MAX_LIMIT = 20


def create_session_memory_router(
    memory_service: MemoryService,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the session memory router.

    :param memory_service: The shared :class:`MemoryService` instance.
    :param conversation_store: Used to check the session exists and to
        resolve the session's owner — the user memory is scoped to.
    :param auth_provider: Auth provider used to identify the requesting
        user for the session-access check. ``None`` in single-user mode.
    :param permission_store: Permission store used to check session-level
        access grants. ``None`` disables permission enforcement.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    async def _resolve_user(
        request: Request, session_id: str, *, required_level: int = LEVEL_READ
    ) -> str:
        """Require session access, then resolve the owner memory is scoped to.

        A session merely *shared* with another user (even at EDIT) never
        exposes its owner's Memory to them: the caller must also BE that
        owner, so another user with a READ or EDIT grant on this session can
        never read or write someone else's claims through it. A session with
        no owner grant (no permission store configured — a single-user
        server) falls back to the reserved single-user identity, same as
        ``omnigent.superchat.memory_tools.tools.resolve_memory_user``, and skips the
        owner-identity check (there is only one user).
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None:
            await require_access(
                user_id, session_id, required_level, permission_store, conversation_store
            )
        # Every store and memory call here is sync (DB reads, embedding round trips, an index
        # write): off the event loop (ADR 0026).
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise session_not_found()
        owner = (
            await asyncio.to_thread(conversation_store.get_session_owner, session_id)
            or RESERVED_USER_LOCAL
        )
        if permission_store is not None and user_id != owner:
            raise OmnigentError(
                f"{user_id!r} is not the owner of session {session_id!r}'s memory",
                code=ErrorCode.FORBIDDEN,
            )
        return owner

    @router.post("/sessions/{session_id}/memory/remember")
    async def remember(
        request: Request,
        session_id: str,
        body: MemoryRememberRequest,
    ) -> dict[str, Any]:
        """Write a durable claim, immediately indexed.

        Reinforces a near-duplicate; supersedes only the claim named by ``replaces_claim_id``.
        """
        owner = await _resolve_user(request, session_id, required_level=LEVEL_EDIT)
        from omnigent.entities import MemoryEvidenceLink

        evidence = [MemoryEvidenceLink(session_id=session_id, item_id="")]
        return await asyncio.to_thread(
            memory_service.remember,
            owner,
            body.text,
            kind=body.kind,
            quote=body.quote,
            evidence=evidence,
            replaces_claim_id=body.replaces_claim_id,
            explicitness=body.explicitness,
        )

    @router.get("/sessions/{session_id}/memory/profile")
    async def profile(
        request: Request,
        session_id: str,
    ) -> dict[str, Any]:
        """The session owner's Memory Profile, wrapped in the delimiter block.

        Read by the runner process (``omnigent/runner/app.py``) every
        ``superside-chat`` turn (docs/context.md) — a plain HTTP
        GET rather than an in-process call since the runner may be a
        separate process with no runtime access.
        """
        owner = await _resolve_user(request, session_id)
        from omnigent.superchat.memory import memory_profile_for

        return {
            "profile": await asyncio.to_thread(memory_profile_for, owner, service=memory_service)
        }

    @router.get("/sessions/{session_id}/memory/search")
    async def search(
        request: Request,
        session_id: str,
        query: str = Query(min_length=1),
        kind: str | None = Query(default=None),
        limit: int = Query(default=_SEARCH_DEFAULT_LIMIT, ge=1, le=_SEARCH_MAX_LIMIT),
    ) -> dict[str, Any]:
        """Hybrid search over the session owner's active claims, with how memory is kept.

        ``memory_upkeep`` carries the upkeep job's cadence, next run and last runs in plain
        words, so the Assistant can say what its memory actually did instead of guessing.
        """
        owner = await _resolve_user(request, session_id)
        results = await asyncio.to_thread(
            memory_service.search, owner, query, kind=kind, limit=limit
        )
        from omnigent.server.memory_upkeep import MEMORY_HOW_IT_WORKS, upkeep_status

        status = await asyncio.to_thread(upkeep_status, request.app.state, owner, runs=3)
        if status is None:
            return {"results": results}
        def iso(epoch: int | None) -> str | None:
            return None if not epoch else datetime.fromtimestamp(epoch, UTC).isoformat()

        upkeep = {
            "how_it_works": MEMORY_HOW_IT_WORKS,
            "next_run_at": iso(status["next_run_at"]),
            "last_runs": [{**run, "at": iso(run["at"])} for run in status["runs"]],
        }
        return {"results": results, "memory_upkeep": upkeep}

    @router.get("/sessions/{session_id}/memory/claims")
    async def list_claims(
        request: Request,
        session_id: str,
        kinds: str | None = Query(default=None, description="Comma-separated claim kinds."),
    ) -> dict[str, Any]:
        """The session owner's active claims (all kinds, or the given ones), newest first."""
        owner = await _resolve_user(request, session_id)
        wanted = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
        return {"claims": await asyncio.to_thread(memory_service.list_claims, owner, kinds=wanted)}

    @router.patch("/sessions/{session_id}/memory/claims/{claim_id}")
    async def edit_claim(
        request: Request,
        session_id: str,
        claim_id: str,
        body: MemoryEditRequest,
    ) -> dict[str, Any]:
        """The person's text edit; the claim becomes person-authored (upkeep never overwrites)."""
        owner = await _resolve_user(request, session_id, required_level=LEVEL_EDIT)
        claim = await asyncio.to_thread(memory_service.edit, owner, claim_id, body.text)
        if claim is None:
            raise OmnigentError("Claim not found", code=ErrorCode.NOT_FOUND)
        return claim

    @router.get("/sessions/{session_id}/memory/claims/{claim_id}")
    async def get_claim(
        request: Request,
        session_id: str,
        claim_id: str,
    ) -> dict[str, Any]:
        """Fetch one claim by id, scoped to the session owner."""
        owner = await _resolve_user(request, session_id)
        claim = await asyncio.to_thread(memory_service.get, owner, claim_id)
        if claim is None:
            raise OmnigentError("Claim not found", code=ErrorCode.NOT_FOUND)
        return claim

    @router.get("/sessions/{session_id}/memory/claims/{claim_id}/explain")
    async def explain_claim(
        request: Request,
        session_id: str,
        claim_id: str,
    ) -> dict[str, Any]:
        """Evidence and supersession chain for one claim."""
        owner = await _resolve_user(request, session_id)
        explanation = await asyncio.to_thread(memory_service.explain, owner, claim_id)
        if explanation is None:
            raise OmnigentError("Claim not found", code=ErrorCode.NOT_FOUND)
        return explanation

    @router.post("/sessions/{session_id}/memory/forget")
    async def forget(
        request: Request,
        session_id: str,
        body: MemoryForgetRequest,
    ) -> dict[str, Any]:
        """Two-step forget: ``confirm=false`` plans, ``confirm=true`` executes."""
        owner = await _resolve_user(request, session_id, required_level=LEVEL_EDIT)
        if body.claim_id is None and not body.query:
            raise OmnigentError("forget requires claim_id or query", code=ErrorCode.INVALID_INPUT)
        return await asyncio.to_thread(
            memory_service.forget,
            owner,
            claim_id=body.claim_id,
            query=body.query,
            confirm=body.confirm,
        )

    return router
