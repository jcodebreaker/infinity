"""SQLAlchemy-backed objective store."""

from __future__ import annotations

import builtins
import json
import uuid

from sqlalchemy import asc, delete, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlObjective,
    SqlObjectiveProposal,
    SqlObjectiveTask,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import Objective, ObjectiveProposal, ObjectiveTask
from omnigent.entities.objective import OBJECTIVE_STATUSES, OBJECTIVE_TASK_STATUSES
from omnigent.stores.objective_store import _UNSET, ObjectiveStore, ProposalNotOpenError


def _task_entity(row: SqlObjectiveTask) -> ObjectiveTask:
    return ObjectiveTask(id=row.id, title=row.title, status=row.status, note=row.note)


def _proposal_entity(row: SqlObjectiveProposal) -> ObjectiveProposal:
    return ObjectiveProposal(
        id=row.id,
        objective_id=row.objective_id,
        reason=row.reason,
        plan=json.loads(row.plan),
        status=row.status,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


def _objective_entity(
    row: SqlObjective,
    tasks: builtins.list[SqlObjectiveTask],
    proposal: SqlObjectiveProposal | None,
) -> Objective:
    return Objective(
        id=row.id,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        user_id=row.user_id,
        parent_session_id=row.parent_session_id,
        title=row.title,
        description=row.description,
        status=row.status,
        due=row.due,
        scheduled_task_id=row.scheduled_task_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        tasks=[_task_entity(t) for t in tasks],
        open_proposal=_proposal_entity(proposal) if proposal is not None else None,
    )


def _load(session: Session, rows: builtins.list[SqlObjective]) -> builtins.list[Objective]:
    """Attach plans and open proposals to objective rows with two batched reads."""
    if not rows:
        return []
    ids = [r.id for r in rows]
    ws = current_workspace_id()
    tasks: dict[str, builtins.list[SqlObjectiveTask]] = {}
    for t in session.execute(
        select(SqlObjectiveTask)
        .where(SqlObjectiveTask.workspace_id == ws, SqlObjectiveTask.objective_id.in_(ids))
        .order_by(asc(SqlObjectiveTask.position), asc(SqlObjectiveTask.id))
    ).scalars():
        tasks.setdefault(t.objective_id, []).append(t)
    proposals = {
        p.objective_id: p
        for p in session.execute(
            select(SqlObjectiveProposal).where(
                SqlObjectiveProposal.workspace_id == ws,
                SqlObjectiveProposal.objective_id.in_(ids),
                SqlObjectiveProposal.status == "open",
            )
        ).scalars()
    }
    return [_objective_entity(r, tasks.get(r.id, []), proposals.get(r.id)) for r in rows]


def _open_proposal_row(session: Session, objective_id: str) -> SqlObjectiveProposal | None:
    return session.execute(
        select(SqlObjectiveProposal).where(
            SqlObjectiveProposal.workspace_id == current_workspace_id(),
            SqlObjectiveProposal.objective_id == objective_id,
            SqlObjectiveProposal.status == "open",
        )
    ).scalar_one_or_none()


class SqlAlchemyObjectiveStore(ObjectiveStore):
    """SQLAlchemy implementation of :class:`ObjectiveStore`."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.objective_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.objective_store", immediate=True
        )

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
        created_at = now_epoch()

        def write(session: Session) -> Objective:
            row = SqlObjective(
                id=objective_id,
                user_id=user_id,
                parent_session_id=parent_session_id,
                title=title,
                description=description,
                status="active",
                due=due,
                scheduled_task_id=scheduled_task_id,
                created_at=created_at,
            )
            session.add(row)
            session.flush()
            return _objective_entity(row, [], None)

        return run_write_transaction(self._session_immediate, "insert_objective", write)

    def get(self, objective_id: str) -> Objective | None:
        """Return an objective with its plan and open proposal, or ``None``."""
        with self._session("select_objective_by_id") as session:
            row = session.get(SqlObjective, (current_workspace_id(), objective_id))
            return _load(session, [row])[0] if row is not None else None

    def get_by_scheduled_task_id(self, scheduled_task_id: str) -> Objective | None:
        """Return the objective advanced by this scheduled task, or ``None``."""
        with self._session("select_objective_by_scheduled_task") as session:
            row = session.execute(
                select(SqlObjective).where(
                    SqlObjective.workspace_id == current_workspace_id(),
                    SqlObjective.scheduled_task_id == scheduled_task_id,
                )
            ).scalar_one_or_none()
            return _load(session, [row])[0] if row is not None else None

    def list(
        self,
        *,
        owner_user_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> builtins.list[Objective]:
        """List objectives (plan and open proposal included), oldest first."""
        with self._session("list_objectives") as session:
            stmt = (
                select(SqlObjective)
                .where(SqlObjective.workspace_id == current_workspace_id())
                .order_by(asc(SqlObjective.created_at), asc(SqlObjective.id))
            )
            if owner_user_id is not None:
                stmt = stmt.where(SqlObjective.user_id == owner_user_id)
            if parent_session_id is not None:
                stmt = stmt.where(SqlObjective.parent_session_id == parent_session_id)
            return _load(session, builtins.list(session.execute(stmt).scalars().all()))

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
        if status is not None and status not in OBJECTIVE_STATUSES:
            raise ValueError(f"invalid objective status {status!r}")
        updated_at = now_epoch()

        def write(session: Session) -> Objective | None:
            row = session.get(SqlObjective, (current_workspace_id(), objective_id))
            if row is None:
                return None
            if title is not None:
                row.title = title
            if description is not None:
                row.description = description
            if status is not None:
                row.status = status
            if due is not _UNSET:
                row.due = due
            if scheduled_task_id is not _UNSET:
                row.scheduled_task_id = scheduled_task_id
            row.updated_at = updated_at
            session.flush()
            return _load(session, [row])[0]

        return run_write_transaction(self._session_immediate, "update_objective", write)

    def update_task(
        self,
        objective_id: str,
        task_id: str,
        *,
        status: str | None = None,
        note: str | None = _UNSET,
    ) -> ObjectiveTask | None:
        """Update one task's status and/or note; ``None`` when it does not exist."""
        if status is not None and status not in OBJECTIVE_TASK_STATUSES:
            raise ValueError(f"invalid task status {status!r}")

        def write(session: Session) -> ObjectiveTask | None:
            row = session.get(SqlObjectiveTask, (current_workspace_id(), task_id))
            if row is None or row.objective_id != objective_id:
                return None
            if status is not None:
                row.status = status
            if note is not _UNSET:
                row.note = note
            objective = session.get(SqlObjective, (current_workspace_id(), objective_id))
            if objective is not None:
                objective.updated_at = now_epoch()
            session.flush()
            return _task_entity(row)

        return run_write_transaction(self._session_immediate, "update_objective_task", write)

    def set_open_proposal(
        self,
        proposal_id: str,
        objective_id: str,
        *,
        reason: str,
        plan: builtins.list[dict[str, str | None]],
    ) -> ObjectiveProposal:
        """Create the objective's open proposal; any previous open one reads ``superseded``."""
        now = now_epoch()

        def write(session: Session) -> ObjectiveProposal:
            ws = current_workspace_id()
            known = set(
                session.execute(
                    select(SqlObjectiveTask.id).where(
                        SqlObjectiveTask.workspace_id == ws,
                        SqlObjectiveTask.objective_id == objective_id,
                    )
                ).scalars()
            )
            for item in plan:
                if item.get("id") is not None and item["id"] not in known:
                    raise ValueError(f"unknown task id {item['id']!r}")
            previous = _open_proposal_row(session, objective_id)
            if previous is not None:
                previous.status = "superseded"
                previous.resolved_at = now
            row = SqlObjectiveProposal(
                id=proposal_id,
                objective_id=objective_id,
                reason=reason,
                plan=json.dumps(plan),
                status="open",
                created_at=now,
            )
            session.add(row)
            session.flush()
            return _proposal_entity(row)

        return run_write_transaction(self._session_immediate, "insert_objective_proposal", write)

    def decided_proposals(
        self, *, owner_user_id: str | None, since: int
    ) -> builtins.list[tuple[str, ObjectiveProposal]]:
        """The owner's accepted or dismissed proposals since *since*, newest first."""
        with self._session("list_decided_objective_proposals") as session:
            owner = (
                SqlObjective.user_id.is_(None)
                if owner_user_id is None
                else SqlObjective.user_id == owner_user_id
            )
            stmt = (
                select(SqlObjective.title, SqlObjectiveProposal)
                .join(
                    SqlObjective,
                    (SqlObjective.workspace_id == SqlObjectiveProposal.workspace_id)
                    & (SqlObjective.id == SqlObjectiveProposal.objective_id),
                )
                .where(
                    SqlObjectiveProposal.workspace_id == current_workspace_id(),
                    SqlObjectiveProposal.status.in_(("accepted", "dismissed")),
                    SqlObjectiveProposal.resolved_at >= since,
                    owner,
                )
                .order_by(SqlObjectiveProposal.resolved_at.desc())
            )
            return [(title, _proposal_entity(row)) for title, row in session.execute(stmt).all()]

    def resolve_proposal(
        self,
        objective_id: str,
        proposal_id: str,
        *,
        accept: bool,
    ) -> Objective | None:
        """Accept (apply the plan atomically) or dismiss an open proposal."""
        now = now_epoch()

        def write(session: Session) -> Objective | None:
            ws = current_workspace_id()
            proposal = session.get(SqlObjectiveProposal, (ws, proposal_id))
            objective = session.get(SqlObjective, (ws, objective_id))
            if proposal is None or objective is None or proposal.objective_id != objective_id:
                return None
            if proposal.status != "open":
                raise ProposalNotOpenError(proposal_id)
            if accept:
                existing = {
                    t.id: t
                    for t in session.execute(
                        select(SqlObjectiveTask).where(
                            SqlObjectiveTask.workspace_id == ws,
                            SqlObjectiveTask.objective_id == objective_id,
                        )
                    ).scalars()
                }
                kept: set[str] = set()
                for position, item in enumerate(json.loads(proposal.plan)):
                    task = existing.get(item.get("id") or "")
                    if task is not None and task.id not in kept:
                        task.title = item["title"]
                        task.position = position
                        kept.add(task.id)
                    else:
                        session.add(
                            SqlObjectiveTask(
                                id=uuid.uuid4().hex,
                                objective_id=objective_id,
                                position=position,
                                title=item["title"],
                                status="pending",
                            )
                        )
                removed = [tid for tid in existing if tid not in kept]
                if removed:
                    session.execute(
                        delete(SqlObjectiveTask).where(
                            SqlObjectiveTask.workspace_id == ws,
                            SqlObjectiveTask.id.in_(removed),
                        )
                    )
            proposal.status = "accepted" if accept else "dismissed"
            proposal.resolved_at = now
            objective.updated_at = now
            session.flush()
            return _load(session, [objective])[0]

        return run_write_transaction(self._session_immediate, "resolve_objective_proposal", write)
