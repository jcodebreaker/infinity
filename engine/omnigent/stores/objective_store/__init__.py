"""Objective store — persists objectives, their plans and plan proposals.

An objective is an outcome pursued over time under a parent session. Its plan
(``objective_tasks``) changes shape only by accepting a proposal
(``objective_proposals``); task progress is updated in place.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import Objective, ObjectiveProposal, ObjectiveTask

# Sentinel meaning "leave the column unchanged"; ``None`` sets it to NULL.
_UNSET: Any = object()


class ProposalNotOpenError(Exception):
    """The proposal was already accepted or dismissed."""


class ObjectiveStore(ABC):
    """Abstract base for objective persistence."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: Backend URI, e.g. ``"sqlite:///chat.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        objective_id: str,
        *,
        user_id: str | None,
        parent_session_id: str,
        title: str,
        description: str = "",
        due: str | None = None,
        scheduled_task_id: str | None = None,
    ) -> Objective:
        """Insert an active objective with an empty plan."""
        ...

    @abstractmethod
    def get(self, objective_id: str) -> Objective | None:
        """Return an objective with its plan and open proposal, or ``None``."""
        ...

    @abstractmethod
    def get_by_scheduled_task_id(self, scheduled_task_id: str) -> Objective | None:
        """Return the objective advanced by this scheduled task, or ``None``."""
        ...

    @abstractmethod
    def list(
        self,
        *,
        owner_user_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> builtins.list[Objective]:
        """List objectives (plan and open proposal included), oldest first."""
        ...

    @abstractmethod
    def update(
        self,
        objective_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        due: str | None = _UNSET,
        scheduled_task_id: str | None = _UNSET,
    ) -> Objective | None:
        """Update mutable fields; ``None`` leaves a field unchanged except ``due``/task id."""
        ...

    @abstractmethod
    def update_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        status: str | None = None,
        note: str | None = _UNSET,
    ) -> ObjectiveTask | None:
        """Update one task's status and/or note; ``None`` when it does not exist."""
        ...

    @abstractmethod
    def set_open_proposal(
        self,
        proposal_id: str,
        objective_id: str,
        *,
        reason: str,
        plan: builtins.list[dict[str, str | None]],
    ) -> ObjectiveProposal:
        """
        Create the objective's open proposal; any previous open one reads ``superseded``.

        :param plan: Ordered ``{"id": kept task id or None, "title": str}`` items.
        :raises ValueError: If a kept id is not a task of this objective.
        """
        ...

    @abstractmethod
    def decided_proposals(
        self, *, owner_user_id: str | None, since: int
    ) -> builtins.list[tuple[str, ObjectiveProposal]]:
        """
        The owner's proposals the owner accepted or dismissed since *since* (epoch seconds),
        newest first, each with its objective's title. Superseded ones are not decisions.

        :param owner_user_id: The objectives' ``user_id`` (``None`` in single-user mode).
        """
        ...

    @abstractmethod
    def resolve_proposal(
        self,
        objective_id: str,
        proposal_id: str,
        *,
        accept: bool,
    ) -> Objective | None:
        """
        Accept (apply the plan atomically) or dismiss an open proposal.

        Kept tasks retain their id, status and note; new items start
        ``pending``; tasks absent from the plan are removed.

        :returns: The updated objective, or ``None`` if the proposal is not this
            objective's.
        :raises ProposalNotOpenError: If the proposal is no longer open.
        """
        ...
