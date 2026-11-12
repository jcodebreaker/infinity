"""Objective entities: an outcome pursued over time, its plan, and plan proposals."""

from __future__ import annotations

from dataclasses import dataclass, field

OBJECTIVE_STATUSES = ("active", "paused", "done", "archived")
OBJECTIVE_TASK_STATUSES = ("pending", "in_progress", "done", "blocked", "skipped")


@dataclass
class ObjectiveTask:
    """
    One plan item of an objective.

    :param id: Bare 32-char hex id.
    :param title: What to do, e.g. ``"Shortlist three venues"``.
    :param status: One of :data:`OBJECTIVE_TASK_STATUSES`.
    :param note: Short progress note, or ``None``.
    """

    id: str
    title: str
    status: str = "pending"
    note: str | None = None


@dataclass
class ObjectiveProposal:
    """
    A suggested full plan awaiting the owner's decision.

    :param plan: Ordered ``{"id": <kept task id or None>, "title": str}`` items.
    :param status: ``open``/``accepted``/``dismissed`` (by the owner)/``superseded`` (replaced).
    """

    id: str
    objective_id: str
    reason: str
    plan: list[dict[str, str | None]]
    status: str
    created_at: int
    resolved_at: int | None = None


@dataclass
class Objective:
    """
    An outcome handed over to be pursued over time.

    :param parent_session_id: The session (Conversation) it belongs to.
    :param due: Optional ISO-8601 date/datetime string.
    :param scheduled_task_id: The scheduled task that advances it, if any.
    :param tasks: The accepted plan, in order.
    :param open_proposal: The pending proposal, if any.
    """

    id: str
    workspace_id: int
    user_id: str | None
    parent_session_id: str
    title: str
    description: str
    status: str
    due: str | None
    scheduled_task_id: str | None
    created_at: int
    updated_at: int | None = None
    tasks: list[ObjectiveTask] = field(default_factory=list)
    open_proposal: ObjectiveProposal | None = None
