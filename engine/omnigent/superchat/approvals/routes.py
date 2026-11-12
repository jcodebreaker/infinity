"""``/v1/me/approvals``: the person's say over what the Muse may do without asking.

* ``GET /me/approvals/pending`` lists the prompts still waiting (any session of theirs).
* ``POST /me/approvals/{id}/answer`` answers one: ``once``, ``always`` (also saves a standing
  rule) or ``deny``. An unanswered prompt stays pending; nothing here ever times out to yes.
* ``/me/approval-rules`` lists, adds and revokes standing rules.
* ``/me/approval-settings`` reads and sets the daily spending cap (``0`` = always ask).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import pending_elicitations
from omnigent.server.auth import LEVEL_EDIT, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access_and_level, require_user
from omnigent.server.routes._sessions.helpers import _apply_pending_policy_ask_writes
from omnigent.server.routes._sessions.orchestration import (
    ON_ELICITATION_ANSWERED,
    _resolve_elicitation,
)
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.superchat.approvals.held import drop_unparked
from omnigent.superchat.approvals.policy import (
    CATEGORIES,
    POLICY_NAME,
    decode_reason,
    rule_label,
)
from omnigent.superchat.approvals.store import (
    LOCAL_OWNER,
    ApprovalRule,
    PendingApproval,
    SqlAlchemyApprovalStore,
)
from omnigent.superchat.risk import target_name

_MAX_CAP_USD = 1_000_000.0
_logger = logging.getLogger(__name__)


class AnswerBody(BaseModel):
    """Body of ``POST /v1/me/approvals/{id}/answer``."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["once", "always", "deny"]


class RuleBody(BaseModel):
    """Body of ``POST /v1/me/approval-rules``."""

    model_config = ConfigDict(extra="forbid")

    category: Literal["send", "post", "delete", "spend", "upload", "share"]
    target: str = Field(min_length=1, max_length=256)
    label: str | None = Field(default=None, max_length=256)
    decision: Literal["allow", "deny"] = "allow"


class SettingsBody(BaseModel):
    """Body of ``PUT /v1/me/approval-settings``."""

    model_config = ConfigDict(extra="forbid")

    daily_cap_usd: float = Field(ge=0, le=_MAX_CAP_USD)


def rule_to_response(rule: ApprovalRule) -> dict[str, Any]:
    """Serialize a standing rule."""
    return {
        "id": rule.id,
        "category": rule.category,
        "target": rule.target,
        "label": rule.label or rule_label(rule.category, rule.target),
        "decision": rule.decision,
        "created_at": rule.created_at,
    }


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _decoded(pending: PendingApproval) -> dict[str, Any]:
    try:
        event = json.loads(pending.event)
    except ValueError:
        return {}
    params = event.get("params") if isinstance(event, dict) else None
    if not isinstance(params, dict):
        return {}
    approval = params.get("approval")  # filed by the mirror from server-attested context
    return approval if isinstance(approval, dict) else {}


def _targets(pending: PendingApproval) -> list[str]:
    return [t for t in _decoded(pending).get("t", []) if isinstance(t, str)]


def target_names(pending: PendingApproval) -> dict[str, str]:
    """The person-facing name of each target (reads stores: call off the event loop)."""
    return {t: target_name(t, pending.user_id) for t in _targets(pending)}


def pending_to_response(
    pending: PendingApproval, cap_usd: float, names: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Serialize a pending approval for the card.

    :param names: :func:`target_names` of *pending*; targets show as stored without it.
    """
    info = _decoded(pending)
    targets = _targets(pending)
    names = names or {}
    category = pending.category
    return {
        "id": pending.elicitation_id,
        "session_id": pending.session_id,
        "category": category,
        "targets": targets,
        "summary": pending.summary,
        "amount_usd": pending.amount_usd,
        "can_always": bool(targets) and (category != "spend" or cap_usd > 0),
        "always_label": ", ".join(rule_label(category, names.get(t, t)) for t in targets),
        "created_at": pending.created_at,
        "arguments": str(info.get("args") or "") or None,
        "also_asks": [str(a) for a in info.get("also", []) if isinstance(a, str)],
    }


async def answer_approval(
    store: SqlAlchemyApprovalStore,
    *,
    owner: str,
    user_id: str | None,
    elicitation_id: str,
    decision: Literal["once", "always", "deny"],
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    permission_store: PermissionStore | None,
) -> None:
    """Answer one waiting approval as *owner*: the single place the decision is applied.

    Used by ``POST /me/approvals/{id}/answer`` and by the decisions inbox
    (``POST /me/asks/{id}/answer``).

    :raises OmnigentError: ``NOT_FOUND`` when the prompt is gone or not the owner's;
        ``INVALID_INPUT`` for ``always`` on a prompt that cannot be allowed every time.
    """
    pending = await asyncio.to_thread(store.get_pending, elicitation_id)
    if pending is None or (pending.user_id or LOCAL_OWNER) != owner:
        raise OmnigentError("Approval not found", code=ErrorCode.NOT_FOUND)
    access = await require_access_and_level(
        user_id, pending.session_id, LEVEL_EDIT, permission_store, conversation_store
    )
    conv = access.conversation or await asyncio.to_thread(
        conversation_store.get_conversation, pending.session_id
    )
    if conv is None:
        raise OmnigentError("Approval not found", code=ErrorCode.NOT_FOUND)
    targets = _targets(pending)
    if decision == "always":
        names = await asyncio.to_thread(target_names, pending)
        cap = await asyncio.to_thread(store.get_cap, owner)
        if not targets or (pending.category == "spend" and cap <= 0):
            raise OmnigentError("This can't be allowed every time", code=ErrorCode.INVALID_INPUT)
        for target in targets:
            await asyncio.to_thread(
                store.create_rule,
                owner,
                category=pending.category,
                target=target,
                label=rule_label(pending.category, names[target]),
            )
    data: dict[str, object] = {
        "elicitation_id": elicitation_id,
        "action": "decline" if decision == "deny" else "accept",
    }
    if decision == "deny":
        # Answered from the inbox, maybe long after the ask: the agent gets "declined" and
        # replies, instead of the turn being stopped.
        data["interrupt"] = False
    await _resolve_elicitation(pending.session_id, data, runner_router, conversation_store)
    # Count the spend only once the verdict reached the waiting call.
    if decision != "deny" and pending.category == "spend" and pending.amount_usd:
        await asyncio.to_thread(store.add_spend, owner, _today(), pending.amount_usd)
    await _apply_pending_policy_ask_writes(
        pending.session_id, conv, conversation_store, agent_store, data
    )
    await asyncio.to_thread(
        store.decide,
        elicitation_id,
        "denied" if decision == "deny" else "approved",
        owner,
    )


def owner_of(user_id: str | None) -> str:
    """The approval-store owner key for an authenticated user (``local`` when auth is off)."""
    return LOCAL_OWNER if user_id in (None, RESERVED_USER_LOCAL) else str(user_id)


def create_approvals_router(
    store: SqlAlchemyApprovalStore,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None = None,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the approvals router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    def _owner(request: Request) -> str:
        return owner_of(require_user(request, auth_provider))

    @router.get("/me/approvals/pending")
    async def list_pending(request: Request) -> dict[str, Any]:
        """The caller's approvals still waiting, oldest first."""
        owner = _owner(request)
        rows = await asyncio.to_thread(store.list_pending, user_id=owner)
        rows = await drop_unparked(store, rows, runner_router)
        cap = await asyncio.to_thread(store.get_cap, owner)
        names = await asyncio.to_thread(lambda: [target_names(r) for r in rows])
        return {
            "approvals": [pending_to_response(r, cap, n) for r, n in zip(rows, names, strict=True)]
        }

    @router.post("/me/approvals/{elicitation_id}/answer")
    async def answer(request: Request, elicitation_id: str, body: AnswerBody) -> dict[str, bool]:
        """Allow once, always allow (saving a rule), or deny a waiting approval."""
        await answer_approval(
            store,
            owner=_owner(request),
            user_id=require_user(request, auth_provider),
            elicitation_id=elicitation_id,
            decision=body.decision,
            conversation_store=conversation_store,
            agent_store=agent_store,
            runner_router=runner_router,
            permission_store=permission_store,
        )
        return {"ok": True}

    @router.get("/me/approval-rules")
    async def list_rules(request: Request) -> dict[str, Any]:
        """The caller's standing rules, newest first."""
        rules = await asyncio.to_thread(store.list_rules, _owner(request))
        return {"rules": [rule_to_response(r) for r in rules]}

    @router.post("/me/approval-rules", status_code=201)
    async def create_rule(request: Request, body: RuleBody) -> dict[str, Any]:
        """Add a standing rule."""
        owner = _owner(request)
        if body.category not in CATEGORIES:
            raise OmnigentError("Unknown category", code=ErrorCode.INVALID_INPUT)
        rule = await asyncio.to_thread(
            store.create_rule,
            owner,
            category=body.category,
            target=body.target.strip(),
            label=body.label or rule_label(body.category, body.target.strip()),
            decision=body.decision,
        )
        return rule_to_response(rule)

    @router.delete("/me/approval-rules/{rule_id}", status_code=204)
    async def delete_rule(request: Request, rule_id: str) -> None:
        """Revoke a standing rule."""
        if not await asyncio.to_thread(store.delete_rule, _owner(request), rule_id):
            raise OmnigentError("Rule not found", code=ErrorCode.NOT_FOUND)

    @router.get("/me/approval-settings")
    async def get_settings(request: Request) -> dict[str, float]:
        """The daily spending cap and what has gone through today."""
        owner = _owner(request)
        return {
            "daily_cap_usd": await asyncio.to_thread(store.get_cap, owner),
            "spent_today_usd": await asyncio.to_thread(store.get_spend, owner, _today()),
        }

    @router.put("/me/approval-settings")
    async def put_settings(request: Request, body: SettingsBody) -> dict[str, float]:
        """Set the daily spending cap (0 = always ask)."""
        owner = _owner(request)
        cap = await asyncio.to_thread(store.set_cap, owner, body.daily_cap_usd)
        return {
            "daily_cap_usd": cap,
            "spent_today_usd": await asyncio.to_thread(store.get_spend, owner, _today()),
        }

    return router


GENERIC_CATEGORY = "tool"


def _filed_from_attested(event: object) -> bool:
    """Whether a stored prompt was filed from server-attested context (``params.approval``)."""
    params = event.get("params") if isinstance(event, dict) else None
    return isinstance(params, dict) and isinstance(params.get("approval"), dict)


def install_pending_persistence(store: SqlAlchemyApprovalStore) -> int:
    """Mirror approval prompts into ``store`` and restore the ones a restart interrupted.

    Only tool-call prompts the server raised itself are filed, from the context it attested
    (:func:`omnigent.runtime.pending_elicitations.attest`): this policy's own reason decoded
    strictly, the owner from the server-set principal. Event text is never trusted. Any other
    policy's ask is filed as a generic one-off ask naming the tool and its arguments.

    :returns: How many pending approvals were put back in the live index.
    """

    def mirror(conversation_id: str, elicitation_id: str, event: dict[str, Any] | None) -> None:
        if event is None:
            # Resolved without an answer recorded here (it timed out or its turn ended): a
            # decision made in the Inbox was recorded first and is kept.
            store.decide(elicitation_id, "expired")
            return
        params = event.get("params")
        if not isinstance(params, dict) or params.get("target_session_id"):
            return  # an ancestor's mirrored copy: the original is filed under its own session
        context = pending_elicitations.attested(elicitation_id)
        if (
            context is None
            or context.get("phase") != "tool_call"
            or context.get("session_id") != conversation_id
        ):
            return
        run_as = context.get("run_as")
        owner = run_as if isinstance(run_as, str) and run_as else LOCAL_OWNER
        reasons = context.get("policy_reasons") or {}
        others = [
            text
            for name, reason in reasons.items()
            if name != POLICY_NAME and (text := str(reason).strip())
        ]
        tool = str(context.get("tool_name") or "")
        info = decode_reason(reasons.get(POLICY_NAME))
        if info is None:
            # The tool's arguments are agent text: shown quoted on the card, never in the title.
            first = others.pop(0) if others else "Approval required"
            category, target, amount = GENERIC_CATEGORY, tool, None
            summary = f"{first} ({tool})" if tool else first
            approval: dict[str, Any] = {}
        else:
            targets = [t for t in info.get("t", []) if isinstance(t, str)]
            raw_amount = info.get("a")
            approval = {"c": info["c"], "t": targets, "s": str(info.get("s") or "")}
            category, target, summary = info["c"], ", ".join(targets), approval["s"]
            amount = float(raw_amount) if isinstance(raw_amount, (int, float)) else None
        # What one Approve covers: every other policy's ask, and the call's arguments.
        approval["also"] = others
        approval["args"] = str(context.get("content_preview") or "")
        stored = {**event, "params": {**params, "approval": approval}}
        store.put_pending(
            elicitation_id,
            conversation_id,
            user_id=owner,
            category=category,
            target=target,
            summary=summary,
            amount_usd=amount,
            event=json.dumps(stored),
        )

    restored = 0
    for row in store.list_pending():
        try:
            event = json.loads(row.event)
        except ValueError:
            event = None
        if _filed_from_attested(event):
            pending_elicitations.restore(row.session_id, event)  # type: ignore[arg-type]
            restored += 1
        else:
            # Filed before prompts were attested (its fields came from event text): drop it.
            store.decide(row.elicitation_id, "expired")
            _logger.info("dropped a legacy pending approval: %s", row.elicitation_id)
    pending_elicitations.set_persist_hook(mirror)

    def answered(
        session_id: str, elicitation_id: str, action: str, answered_by: str | None
    ) -> None:
        # Answered outside the Inbox (the session's resolve URL or approval event): the
        # resolution's ``expired`` above is replaced by the decision and who made it.
        store.decide(
            elicitation_id,
            "approved" if action == "accept" else "denied",
            owner_of(answered_by),
            session_id=session_id,
        )

    ON_ELICITATION_ANSWERED["approvals"] = answered
    return restored
