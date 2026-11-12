"""Shape the three kinds of decision into one ``ask`` (pure; no copy, no I/O).

An ask is ``{id, kind, session_id, objective_id, created_at, subject, choices}``. Wording stays
with the client: ``kind`` and the choice ids are stable codes it localizes.

Ask ids encode the kind, so an answer needs no lookup table:

* ``approval:<elicitation_id>``
* ``proposal:<objective_id>:<proposal_id>``
* ``task:<objective_id>:<task_id>``

Choice ids (``style`` is ``primary`` / ``secondary`` / ``danger``):

* approval: ``approve_once`` (primary), ``approve_always`` (secondary, only when ``can_always``),
  ``deny`` (danger);
* plan_proposal: ``accept`` (primary), ``dismiss`` (secondary; the client says "Not now" for a
  first plan, "Keep current" otherwise: ``subject.is_first_plan``). ``keep_current`` is accepted
  as an alias of ``dismiss`` on answer;
* blocked_task: ``answer`` (primary), which needs a ``note`` (the person's reply).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnigent.entities import Objective
from omnigent.superchat.approvals.routes import pending_to_response
from omnigent.superchat.approvals.store import PendingApproval

KIND_APPROVAL = "approval"
KIND_PLAN_PROPOSAL = "plan_proposal"
KIND_BLOCKED_TASK = "blocked_task"

_APPROVAL_DECISIONS = {"approve_once": "once", "approve_always": "always", "deny": "deny"}
_PROPOSAL_CHOICES = {"accept": True, "dismiss": False, "keep_current": False}


def approval_ask_id(elicitation_id: str) -> str:
    """Ask id of a pending approval."""
    return f"approval:{elicitation_id}"


def parse_ask_id(ask_id: str) -> tuple[str, list[str]] | None:
    """``(kind_prefix, parts)`` of an ask id, or ``None`` when it is not one of ours."""
    prefix, _, rest = ask_id.partition(":")
    if prefix == "approval" and rest:
        return prefix, [rest]
    if prefix in ("proposal", "task"):
        parts = rest.split(":")
        if len(parts) == 2 and all(parts):
            return prefix, parts
    return None


def approval_decision(choice: str) -> str | None:
    """The approval store's decision for a choice id, or ``None`` for an unknown choice."""
    return _APPROVAL_DECISIONS.get(choice)


def proposal_accepts(choice: str) -> bool | None:
    """Whether a choice id accepts the proposal (``None`` for an unknown choice)."""
    return _PROPOSAL_CHOICES.get(choice)


def approval_ask(
    pending: PendingApproval, cap_usd: float, names: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """An approval waiting on the person (*names*: its targets' ``target_names``)."""
    row = pending_to_response(pending, cap_usd, names)
    choices = [{"id": "approve_once", "style": "primary"}]
    if row["can_always"]:
        choices.append({"id": "approve_always", "style": "secondary"})
    choices.append({"id": "deny", "style": "danger"})
    return {
        "id": approval_ask_id(pending.elicitation_id),
        "kind": KIND_APPROVAL,
        "session_id": pending.session_id,
        "objective_id": None,
        "created_at": pending.created_at,
        "subject": {
            "category": row["category"],
            "targets": row["targets"],
            "summary": row["summary"],
            "amount_usd": row["amount_usd"],
            "can_always": row["can_always"],
            "always_label": row["always_label"],
            "arguments": row["arguments"],
            "also_asks": row["also_asks"],
        },
        "choices": choices,
    }


def objective_asks(objective: Objective) -> list[dict[str, Any]]:
    """The open plan proposal and the blocked tasks of one active objective."""
    asks: list[dict[str, Any]] = []
    base = {
        "session_id": objective.parent_session_id,
        "objective_id": objective.id,
    }
    proposal = objective.open_proposal
    if proposal is not None:
        asks.append(
            {
                "id": f"proposal:{objective.id}:{proposal.id}",
                "kind": KIND_PLAN_PROPOSAL,
                **base,
                "created_at": proposal.created_at,
                "subject": {
                    "objective_title": objective.title,
                    "reason": proposal.reason,
                    "is_first_plan": not objective.tasks,
                    "plan": [{"id": i.get("id"), "title": i.get("title")} for i in proposal.plan],
                },
                "choices": [
                    {"id": "accept", "style": "primary"},
                    {"id": "dismiss", "style": "secondary"},
                ],
            }
        )
    for task in objective.tasks:
        if task.status != "blocked":
            continue
        asks.append(
            {
                "id": f"task:{objective.id}:{task.id}",
                "kind": KIND_BLOCKED_TASK,
                **base,
                "created_at": objective.updated_at or objective.created_at,
                "subject": {
                    "objective_title": objective.title,
                    "task_id": task.id,
                    "title": task.title,
                    "note": task.note,
                },
                "choices": [{"id": "answer", "style": "primary"}],
            }
        )
    return asks
