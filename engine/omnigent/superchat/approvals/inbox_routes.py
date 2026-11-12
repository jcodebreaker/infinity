"""``/v1/me/asks``: the decisions waiting on the person, and how to answer them.

* ``GET /me/asks`` -> ``{"data": [ask]}``, newest first: pending approvals, open plan proposals
  and blocked tasks of the caller's active Goals (see :mod:`omnigent.superchat.approvals.asks`),
  redacted of the secrets the engine knows (:mod:`omnigent.server.redaction`).
* ``POST /me/asks/{id}/answer`` ``{choice, note?}`` -> ``{"ok": true}``. Dispatches to the
  approval answer function and the objective store, the same code the underlying routes run;
  ownership is checked exactly as they do.
* ``GET /me/asks/history`` -> ``{"data": [decision]}``, newest first: the asks answered in the
  last :data:`HISTORY_DAYS` days, read from the stores that hold them (ADR 0036, amended):
  approvals ``approved`` / ``denied`` and plan proposals ``accepted`` / ``dismissed``. A
  decision is ``{id, kind, title, decision, decided_by, by_you, decided_at}``. A blocked task's
  answer is not listed: its store keeps no time for it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.entities import Objective
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.redaction import request_redactor
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.objective_store import ObjectiveStore, ProposalNotOpenError
from omnigent.superchat.approvals.asks import (
    approval_ask,
    approval_decision,
    objective_asks,
    parse_ask_id,
    proposal_accepts,
)
from omnigent.superchat.approvals.held import drop_unparked
from omnigent.superchat.approvals.routes import answer_approval, owner_of, target_names

_NOTE_MAX = 2000
#: How far back the Inbox's History reads.
HISTORY_DAYS = 30


class AskAnswer(BaseModel):
    """Body of ``POST /v1/me/asks/{id}/answer``."""

    model_config = ConfigDict(extra="forbid")

    choice: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=_NOTE_MAX)


def _not_found() -> OmnigentError:
    return OmnigentError("Ask not found", code=ErrorCode.NOT_FOUND)


def create_asks_router(
    *,
    objective_store: ObjectiveStore | None,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    permission_store: PermissionStore | None,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    """Build the asks router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    def _objective_owner(user_id: str | None) -> str | None:
        return None if user_id in (None, RESERVED_USER_LOCAL) else user_id

    def _owned_objective(objective_id: str, user_id: str | None) -> Objective:
        if objective_store is None:
            raise _not_found()
        try:
            uuid_to_bytes(objective_id)
            objective = objective_store.get(objective_id)
        except InvalidUuidError:
            objective = None
        if objective is None or objective.user_id != _objective_owner(user_id):
            raise _not_found()
        return objective

    @router.get("/me/asks")
    async def list_asks(request: Request) -> dict[str, Any]:
        """Everything waiting on the caller, newest first."""
        user_id = require_user(request, auth_provider)
        asks: list[dict[str, Any]] = []
        approvals = getattr(request.app.state, "approval_store", None)
        if approvals is not None:
            owner = owner_of(user_id)
            rows = await asyncio.to_thread(approvals.list_pending, user_id=owner)
            rows = await drop_unparked(approvals, rows, runner_router)
            cap = await asyncio.to_thread(approvals.get_cap, owner)
            names = await asyncio.to_thread(lambda: [target_names(r) for r in rows])
            asks += [approval_ask(r, cap, n) for r, n in zip(rows, names, strict=True)]
        if objective_store is not None:
            owner_id = _objective_owner(user_id)
            objectives = await asyncio.to_thread(
                lambda: objective_store.list(owner_user_id=owner_id)
            )
            for objective in objectives:
                if objective.user_id == owner_id and objective.status == "active":
                    asks += objective_asks(objective)
        asks.sort(key=lambda ask: (ask["created_at"] or 0, ask["id"]), reverse=True)
        return request_redactor(request, user_id).deep({"data": asks})

    @router.get("/me/asks/history")
    async def ask_history(request: Request) -> dict[str, Any]:
        """The caller's answered asks of the last :data:`HISTORY_DAYS` days, newest first."""
        user_id = require_user(request, auth_provider)
        since = int(time.time()) - HISTORY_DAYS * 86400
        owner = owner_of(user_id)
        rows: list[dict[str, Any]] = []
        approvals = getattr(request.app.state, "approval_store", None)
        if approvals is not None:
            decided = await asyncio.to_thread(approvals.list_decided, user_id=owner, since=since)
            rows += [
                {
                    "id": f"approval:{row.elicitation_id}",
                    "kind": "approval",
                    "title": row.summary,
                    "decision": row.status,
                    "decided_by": row.decided_by,
                    "by_you": row.decided_by == owner,
                    "decided_at": row.decided_at,
                }
                for row in decided
            ]
        if objective_store is not None:
            owner_id = _objective_owner(user_id)
            proposals = await asyncio.to_thread(
                lambda: objective_store.decided_proposals(owner_user_id=owner_id, since=since)
            )
            # Only the Goal's owner can answer its proposal, so the owner decided it.
            rows += [
                {
                    "id": f"proposal:{proposal.objective_id}:{proposal.id}",
                    "kind": "plan_proposal",
                    "title": title,
                    "decision": proposal.status,
                    "decided_by": owner,
                    "by_you": True,
                    "decided_at": proposal.resolved_at,
                }
                for title, proposal in proposals
            ]
        rows.sort(key=lambda row: (row["decided_at"] or 0, row["id"]), reverse=True)
        return request_redactor(request, user_id).deep({"data": rows})

    @router.post("/me/asks/{ask_id}/answer")
    async def answer_ask(request: Request, ask_id: str, body: AskAnswer) -> dict[str, bool]:
        """Answer one ask with one of its choice ids."""
        user_id = require_user(request, auth_provider)
        parsed = parse_ask_id(ask_id)
        if parsed is None:
            raise _not_found()
        prefix, parts = parsed
        if prefix == "approval":
            decision = approval_decision(body.choice)
            approvals = getattr(request.app.state, "approval_store", None)
            if approvals is None:
                raise _not_found()
            if decision is None:
                raise OmnigentError("Unknown choice", code=ErrorCode.INVALID_INPUT)
            await answer_approval(
                approvals,
                owner=owner_of(user_id),
                user_id=user_id,
                elicitation_id=parts[0],
                decision=decision,  # type: ignore[arg-type]
                conversation_store=conversation_store,
                agent_store=agent_store,
                runner_router=runner_router,
                permission_store=permission_store,
            )
            return {"ok": True}
        objective_id, item_id = parts
        objective = _owned_objective(objective_id, user_id)
        assert objective_store is not None
        if prefix == "proposal":
            accept = proposal_accepts(body.choice)
            if accept is None:
                raise OmnigentError("Unknown choice", code=ErrorCode.INVALID_INPUT)
            try:
                uuid_to_bytes(item_id)
                resolved = await asyncio.to_thread(
                    lambda: objective_store.resolve_proposal(objective_id, item_id, accept=accept)
                )
            except InvalidUuidError:
                resolved = None
            except ProposalNotOpenError:
                raise OmnigentError(
                    "proposal is no longer open", code=ErrorCode.CONFLICT
                ) from None
            if resolved is None:
                raise _not_found()
            return {"ok": True}
        # Blocked task: the reply goes into its note and the task goes back to pending, which
        # is how the Goal's next run picks it up.
        if body.choice != "answer":
            raise OmnigentError("Unknown choice", code=ErrorCode.INVALID_INPUT)
        reply = (body.note or "").strip()
        if not reply:
            raise OmnigentError("An answer needs a note", code=ErrorCode.INVALID_INPUT)
        task = next(
            (t for t in objective.tasks if t.id == item_id and t.status == "blocked"), None
        )
        if task is None:
            raise _not_found()
        note = f"{task.note}\n\nAnswer: {reply}" if task.note else f"Answer: {reply}"
        await asyncio.to_thread(
            lambda: objective_store.update_task(
                objective_id, item_id, status="pending", note=note[:_NOTE_MAX]
            )
        )
        return {"ok": True}

    return router
