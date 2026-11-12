"""Where work-queue jobs live: Postgres (``work_jobs``) in the engine, memory without a database.

Both stores share :func:`pick`, the one rule for which due jobs may start: the global limit
first, then the owner's limit (and the job's own ``owner_limit``). The SQL store claims under
``SELECT … FOR UPDATE SKIP LOCKED`` (``BEGIN IMMEDIATE`` on SQLite), so two workers never claim
the same row; the in-memory store, used only by embedders and tests without a database, holds a
lock instead and survives nothing.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlWorkJob, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker

QUEUED = "queued"
CLAIMED = "claimed"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
#: States that hold a slot.
IN_FLIGHT = (CLAIMED, RUNNING)
#: States a submit with the same key re-arms instead of returning.
FINISHED = (DONE, FAILED)

#: How many due rows one claim looks at.
_SCAN = 50


@dataclass(frozen=True)
class Job:
    """One job as handlers and the worker see it.

    :param payload: The JSON object it was submitted with (or a deferral replaced it with).
    :param attempts: How many times it was claimed, this claim included.
    """

    id: str
    workspace_id: int
    kind: str
    key: str
    owner: str
    payload: dict[str, Any] = field(default_factory=dict)
    state: str = QUEUED
    conversation_id: str | None = None
    owner_limit: int | None = None
    not_before: int = 0
    attempts: int = 0
    run_id: str | None = None
    claimed_by: str | None = None
    lease_until: int | None = None
    created_at: int = 0
    updated_at: int = 0


OwnerCap = Callable[[Job], int]


def pick(
    due: Iterable[Job],
    owner_counts: dict[tuple[int, str], int],
    total: int,
    global_limit: int,
    owner_cap: OwnerCap,
) -> list[Job]:
    """The due jobs (in order) that may start now without breaking a limit.

    :param owner_counts: In-flight jobs per ``(workspace_id, owner)``; updated as jobs are picked.
    :param total: In-flight jobs across everyone.
    :param owner_cap: The owner's limit for a job (lower while their turn runs).
    """
    chosen: list[Job] = []
    for job in due:
        if total >= global_limit:
            break
        owner = (job.workspace_id, job.owner)
        cap = owner_cap(job)
        if job.owner_limit is not None:
            cap = min(cap, job.owner_limit)
        if owner_counts.get(owner, 0) >= cap:
            continue
        chosen.append(job)
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
        total += 1
    return chosen


def new_job_id() -> str:
    """A bare 32-char hex id, the store's id convention."""
    return uuid.uuid4().hex


def _to_job(row: SqlWorkJob) -> Job:
    try:
        payload = json.loads(row.payload)
    except ValueError:
        payload = {}
    return Job(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        key=row.key,
        owner=row.owner,
        payload=payload if isinstance(payload, dict) else {},
        state=row.state,
        conversation_id=row.conversation_id,
        owner_limit=row.owner_limit,
        not_before=row.not_before,
        attempts=row.attempts,
        run_id=row.run_id,
        claimed_by=row.claimed_by,
        lease_until=row.lease_until,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyWorkQueueStore:
    """The ``work_jobs`` table. Claims and counts span every workspace (one worker serves all)."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI.
        """
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.work_queue"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.work_queue", immediate=True
        )

    def put(self, job: Job) -> tuple[str, bool]:
        """Insert *job*, or return the open job with its ``(kind, key)``.

        A finished job with that key is re-armed with *job*'s fields (same id).

        :returns: ``(job_id, created)``.
        """
        ws = current_workspace_id()

        def find(session: Session) -> SqlWorkJob | None:
            return session.execute(
                select(SqlWorkJob)
                .where(
                    SqlWorkJob.workspace_id == ws,
                    SqlWorkJob.kind == job.kind,
                    SqlWorkJob.key == job.key,
                )
                .with_for_update()
            ).scalar_one_or_none()

        for _ in range(2):  # a concurrent insert of the same key loses once, then finds it
            try:
                with self._session_immediate("put") as session:
                    row = find(session)
                    if row is not None and row.state not in FINISHED:
                        return row.id, False
                    if row is not None:
                        row.owner = job.owner
                        row.conversation_id = job.conversation_id
                        row.payload = json.dumps(job.payload)
                        row.state = QUEUED
                        row.owner_limit = job.owner_limit
                        row.not_before = job.not_before
                        row.attempts = 0
                        row.lease_until = None
                        row.claimed_by = None
                        row.run_id = None
                        row.error = None
                        row.updated_at = job.updated_at
                        return row.id, True
                    session.add(
                        SqlWorkJob(
                            workspace_id=ws,
                            id=job.id,
                            kind=job.kind,
                            key=job.key,
                            owner=job.owner,
                            conversation_id=job.conversation_id,
                            payload=json.dumps(job.payload),
                            state=QUEUED,
                            owner_limit=job.owner_limit,
                            not_before=job.not_before,
                            attempts=0,
                            created_at=job.created_at,
                            updated_at=job.updated_at,
                        )
                    )
                    session.flush()
                    return job.id, True
            except IntegrityError:
                continue
        raise RuntimeError(f"work queue: could not store job {job.kind}:{job.key}")

    def claim(
        self,
        *,
        now: int,
        kinds: Iterable[str],
        global_limit: int,
        owner_cap: OwnerCap,
        claimer: str,
        lease_until: int,
    ) -> list[Job]:
        """Claim the due jobs of *kinds* that fit the limits; each is bound to *claimer*."""
        kinds = list(kinds)
        if not kinds:
            return []
        with self._session_immediate("claim") as session:
            rows = (
                session.execute(
                    select(SqlWorkJob)
                    .where(
                        SqlWorkJob.state == QUEUED,
                        SqlWorkJob.not_before <= now,
                        SqlWorkJob.kind.in_(kinds),
                    )
                    .order_by(SqlWorkJob.not_before, SqlWorkJob.created_at)
                    .limit(_SCAN)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            if not rows:
                return []
            counts: dict[tuple[int, str], int] = {}
            for ws, owner, n in session.execute(
                select(SqlWorkJob.workspace_id, SqlWorkJob.owner, func.count())
                .where(SqlWorkJob.state.in_(IN_FLIGHT))
                .group_by(SqlWorkJob.workspace_id, SqlWorkJob.owner)
            ):
                counts[(ws, owner)] = n
            by_id = {(r.workspace_id, r.id): r for r in rows}
            chosen = pick(
                [_to_job(r) for r in rows], counts, sum(counts.values()), global_limit, owner_cap
            )
            claimed: list[Job] = []
            for job in chosen:
                row = by_id[(job.workspace_id, job.id)]
                row.state = CLAIMED
                row.claimed_by = claimer
                row.lease_until = lease_until
                row.attempts += 1
                row.updated_at = now
                claimed.append(_to_job(row))
            return claimed

    def _set(self, name: str, ws: int, job_id: str, only: Iterable[str], **values: Any) -> bool:
        with self._session(name) as session:
            result = session.execute(
                update(SqlWorkJob)
                .where(
                    SqlWorkJob.workspace_id == ws,
                    SqlWorkJob.id == job_id,
                    SqlWorkJob.state.in_(list(only)),
                )
                .values(**values)
            )
            return bool(result.rowcount)

    def bind(self, job: Job, run_id: str, now: int) -> bool:
        """The claimed job started *run_id*; it holds its slot until that run ends."""
        return self._set(
            "bind",
            job.workspace_id,
            job.id,
            (CLAIMED,),
            state=RUNNING,
            run_id=run_id,
            lease_until=None,
            updated_at=now,
        )

    def requeue(self, job: Job, not_before: int, payload: dict[str, Any], now: int) -> bool:
        """Put a claimed job back to wait until *not_before* (a deferral or a retry)."""
        return self._set(
            "requeue",
            job.workspace_id,
            job.id,
            (CLAIMED,),
            state=QUEUED,
            not_before=not_before,
            payload=json.dumps(payload),
            claimed_by=None,
            lease_until=None,
            updated_at=now,
        )

    def finish(self, ws: int, job_id: str, state: str, now: int, error: str | None = None) -> bool:
        """End a claimed or running job as ``done`` or ``failed``."""
        return self._set(
            "finish",
            ws,
            job_id,
            IN_FLIGHT,
            state=state,
            error=(error or None) and error[:255],
            lease_until=None,
            updated_at=now,
        )

    def renew(self, claimer: str, ids: Iterable[tuple[int, str]], lease_until: int) -> None:
        """Extend this process's claims that are still launching."""
        for ws, job_id in ids:
            with self._session("renew") as session:
                session.execute(
                    update(SqlWorkJob)
                    .where(
                        SqlWorkJob.workspace_id == ws,
                        SqlWorkJob.id == job_id,
                        SqlWorkJob.state == CLAIMED,
                        SqlWorkJob.claimed_by == claimer,
                    )
                    .values(lease_until=lease_until)
                )

    def expire_claims(self, now: int) -> int:
        """Return claims whose process died before binding a run to the queue."""
        with self._session("expire_claims") as session:
            result = session.execute(
                update(SqlWorkJob)
                .where(SqlWorkJob.state == CLAIMED, SqlWorkJob.lease_until < now)
                .values(
                    state=QUEUED, claimed_by=None, lease_until=None, not_before=now, updated_at=now
                )
            )
            return int(result.rowcount or 0)

    def running(self) -> list[Job]:
        """Every job bound to a run, across workspaces."""
        with self._session("running") as session:
            rows = session.execute(select(SqlWorkJob).where(SqlWorkJob.state == RUNNING))
            return [_to_job(r) for r in rows.scalars().all()]

    def prune(self, before: int) -> int:
        """Delete finished jobs last touched before *before*."""
        with self._session("prune") as session:
            result = session.execute(
                delete(SqlWorkJob).where(
                    SqlWorkJob.state.in_(FINISHED), SqlWorkJob.updated_at < before
                )
            )
            return int(result.rowcount or 0)

    def get(self, ws: int, job_id: str) -> Job | None:
        """One job, or ``None``."""
        with self._session("get") as session:
            row = session.execute(
                select(SqlWorkJob).where(SqlWorkJob.workspace_id == ws, SqlWorkJob.id == job_id)
            ).scalar_one_or_none()
            return _to_job(row) if row is not None else None


class MemoryWorkQueueStore:
    """The same contract in memory, for embedders and tests without a database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[tuple[int, str], Job] = {}

    def put(self, job: Job) -> tuple[str, bool]:
        ws = current_workspace_id()
        with self._lock:
            for (jws, _), row in self._jobs.items():
                if jws == ws and row.kind == job.kind and row.key == job.key:
                    if row.state not in FINISHED:
                        return row.id, False
                    self._jobs[(ws, row.id)] = replace(
                        job, id=row.id, workspace_id=ws, created_at=row.created_at
                    )
                    return row.id, True
            self._jobs[(ws, job.id)] = replace(job, workspace_id=ws, state=QUEUED)
            return job.id, True

    def claim(
        self,
        *,
        now: int,
        kinds: Iterable[str],
        global_limit: int,
        owner_cap: OwnerCap,
        claimer: str,
        lease_until: int,
    ) -> list[Job]:
        kinds = set(kinds)
        with self._lock:
            due = sorted(
                (
                    j
                    for j in self._jobs.values()
                    if j.state == QUEUED and j.not_before <= now and j.kind in kinds
                ),
                key=lambda j: (j.not_before, j.created_at),
            )[:_SCAN]
            counts: dict[tuple[int, str], int] = {}
            for j in self._jobs.values():
                if j.state in IN_FLIGHT:
                    counts[(j.workspace_id, j.owner)] = (
                        counts.get((j.workspace_id, j.owner), 0) + 1
                    )
            chosen = pick(due, counts, sum(counts.values()), global_limit, owner_cap)
            claimed = []
            for job in chosen:
                updated = replace(
                    job,
                    state=CLAIMED,
                    claimed_by=claimer,
                    lease_until=lease_until,
                    attempts=job.attempts + 1,
                    updated_at=now,
                )
                self._jobs[(job.workspace_id, job.id)] = updated
                claimed.append(updated)
            return claimed

    def _set(self, ws: int, job_id: str, only: Iterable[str], **values: Any) -> bool:
        with self._lock:
            row = self._jobs.get((ws, job_id))
            if row is None or row.state not in tuple(only):
                return False
            self._jobs[(ws, job_id)] = replace(row, **values)
            return True

    def bind(self, job: Job, run_id: str, now: int) -> bool:
        return self._set(
            job.workspace_id,
            job.id,
            (CLAIMED,),
            state=RUNNING,
            run_id=run_id,
            lease_until=None,
            updated_at=now,
        )

    def requeue(self, job: Job, not_before: int, payload: dict[str, Any], now: int) -> bool:
        return self._set(
            job.workspace_id,
            job.id,
            (CLAIMED,),
            state=QUEUED,
            not_before=not_before,
            payload=dict(payload),
            claimed_by=None,
            lease_until=None,
            updated_at=now,
        )

    def finish(
        self,
        ws: int,
        job_id: str,
        state: str,
        now: int,
        error: str | None = None,  # noqa: ARG002 - kept in the table only
    ) -> bool:
        return self._set(ws, job_id, IN_FLIGHT, state=state, lease_until=None, updated_at=now)

    def renew(self, claimer: str, ids: Iterable[tuple[int, str]], lease_until: int) -> None:
        with self._lock:
            for key in ids:
                row = self._jobs.get(key)
                if row is not None and row.state == CLAIMED and row.claimed_by == claimer:
                    self._jobs[key] = replace(row, lease_until=lease_until)

    def expire_claims(self, now: int) -> int:
        with self._lock:
            expired = [
                k
                for k, j in self._jobs.items()
                if j.state == CLAIMED and j.lease_until is not None and j.lease_until < now
            ]
            for k in expired:
                self._jobs[k] = replace(
                    self._jobs[k],
                    state=QUEUED,
                    claimed_by=None,
                    lease_until=None,
                    not_before=now,
                    updated_at=now,
                )
            return len(expired)

    def running(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.state == RUNNING]

    def prune(self, before: int) -> int:
        with self._lock:
            old = [
                k for k, j in self._jobs.items() if j.state in FINISHED and j.updated_at < before
            ]
            for k in old:
                del self._jobs[k]
            return len(old)

    def get(self, ws: int, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get((ws, job_id))
