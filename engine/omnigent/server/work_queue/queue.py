"""The worker: claims due jobs within the limits, runs their handlers, and follows their runs.

A handler is ``async fn(job) -> str | Defer | None``:

* ``None`` — the work finished inside the handler (an engine-side model call); the job is done.
* a run id (a session id) — the handler started a run that goes on without it (a Helper). The
  job holds its slot, bound to that run, until the run's turn ends (:meth:`WorkQueue.run_ended`,
  from the session's terminal status edge) or the reaper finds it ended or stale.
* :class:`Defer` — not now: the job waits until ``at`` (quiet hours, a retry after a failed
  launch), stored, so a restart keeps it.

An exception fails the job. A claim whose process died before it bound a run returns to the
queue when its lease expires; a live process renews its own claims, so a slow launch is never
claimed twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from omnigent.db.db_models import workspace_scope
from omnigent.server.work_queue.store import DONE, FAILED, Job, MemoryWorkQueueStore, new_job_id

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Defer:
    """A handler's answer for "not now": wait until *at*; *payload*, when set, replaces it.

    :param at: Epoch seconds the job may be claimed again.
    :param payload: The job's payload from now on (e.g. the next attempt's number).
    """

    at: float
    payload: dict[str, Any] | None = None


Handler = Callable[[Job], Awaitable["str | Defer | None"]]
#: ``fn(run_id) -> status``: ``"missing"`` when the run's session is gone, else its live status
#: (``""`` before its first turn). Also read for a job's Conversation (``running`` = mid-turn).
RunStatus = Callable[[str], str]
#: ``fn(workspace_id, owner) -> int | None``: how many runs the owner's host has room for now.
OwnerRoom = Callable[[int, str], "int | None"]

#: Global limit on jobs in flight across everyone.
GLOBAL_LIMIT_ENV = "OMNIGENT_WORK_QUEUE_MAX_RUNNING"
DEFAULT_GLOBAL_LIMIT = 16
#: Per-owner limit (the existing automation setting); else the host's room, else the default.
OWNER_LIMIT_ENV = "OMNIGENT_AUTOMATION_MAX_CONCURRENT"
DEFAULT_OWNER_LIMIT = 3
#: A claim not bound to a run within this long is presumed dead (its process renews it).
LEASE_SECONDS = 600
#: A bound run still going after this long is released as stale (the run reconciler's age).
RUN_MAX_AGE_SECONDS = 6 * 60 * 60
#: A run reads as ended only once it had time to start its turn.
RUN_START_GRACE_SECONDS = 120
POLL_SECONDS = 5.0
REAP_SECONDS = 60.0
KEEP_FINISHED_SECONDS = 7 * 86400

# kind -> handler; module-level so features register before (or without) the worker starting.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by job kind, not tenant data
_HANDLERS: dict[str, Handler] = {}
_ROOM: list[OwnerRoom] = []


def register_handler(kind: str, fn: Handler) -> None:
    """Run jobs of *kind* with ``fn(job)``; a later registration replaces an earlier one."""
    _HANDLERS[kind] = fn


def set_owner_room(fn: OwnerRoom | None) -> None:
    """Let ``fn(workspace_id, owner)`` size each owner's limit from their host's load."""
    _ROOM[:] = [fn] if fn is not None else []


def global_limit() -> int:
    """Jobs in flight across everyone: ``OMNIGENT_WORK_QUEUE_MAX_RUNNING``, default 16."""
    try:
        return max(1, int(os.environ[GLOBAL_LIMIT_ENV]))
    except (KeyError, ValueError):
        return DEFAULT_GLOBAL_LIMIT


def owner_limit(measured: int | None = None) -> int:
    """Jobs one owner may have in flight while their own turn is not running.

    ``OMNIGENT_AUTOMATION_MAX_CONCURRENT`` fixes it when set; else ``measured`` (what the owner's
    host has room for right now), else 3.
    """
    try:
        return max(1, int(os.environ[OWNER_LIMIT_ENV]))
    except (KeyError, ValueError):
        return max(1, measured) if measured is not None else DEFAULT_OWNER_LIMIT


def _no_status(_run_id: str) -> str:
    return ""


class WorkQueue:
    """One process's worker over a job store."""

    def __init__(
        self,
        store: Any,
        *,
        run_status: RunStatus | None = None,
        poll_s: float = POLL_SECONDS,
    ) -> None:
        """
        :param store: :class:`~omnigent.server.work_queue.store.SqlAlchemyWorkQueueStore` (or the
            in-memory one).
        :param run_status: Reads a session's live status (see :data:`RunStatus`); without it a
            bound run ends only through :meth:`run_ended` or its age.
        """
        self.store = store
        self._run_status = run_status or _no_status
        self._poll_s = poll_s
        self.claimer = f"wq-{uuid.uuid4().hex[:12]}"
        # Bound runs this process knows: run id -> (workspace id, job id). Refreshed by the reaper.
        self._bound: dict[str, tuple[int, str]] = {}
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._claim_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._last_reap = 0.0

    # ── submit ─────────────────────────────────────────────────────────────────

    def put(
        self,
        owner: str,
        kind: str,
        key: str,
        payload: dict[str, Any] | None = None,
        not_before: float | None = None,
        *,
        conversation_id: str | None = None,
        owner_limit: int | None = None,
    ) -> tuple[str, bool]:
        """Blocking: :func:`submit`, also telling whether a new job was queued.

        :returns: ``(job_id, created)``; ``created`` is ``False`` when a job with the same
            ``(kind, key)`` was already open (it is returned unchanged).
        """
        now = int(time.time())
        job = Job(
            id=new_job_id(),
            workspace_id=0,
            kind=kind,
            key=key[:255],
            owner=owner,
            payload=dict(payload or {}),
            conversation_id=conversation_id,
            owner_limit=owner_limit,
            not_before=int(not_before) if not_before is not None else now,
            created_at=now,
            updated_at=now,
        )
        job_id, created = self.store.put(job)
        if created:
            self.kick()
        return job_id, created

    def submit(
        self,
        owner: str,
        kind: str,
        key: str,
        payload: dict[str, Any] | None = None,
        not_before: float | None = None,
        *,
        conversation_id: str | None = None,
        owner_limit: int | None = None,
    ) -> str:
        """Blocking: queue a job; idempotent on ``(kind, key)`` while that job is open."""
        return self.put(
            owner,
            kind,
            key,
            payload,
            not_before,
            conversation_id=conversation_id,
            owner_limit=owner_limit,
        )[0]

    def kick(self) -> None:
        """Wake the worker now (from any thread)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            if asyncio.get_running_loop() is loop:
                self._wake.set()
                return
        except RuntimeError:
            pass
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._wake.set)

    # ── limits ─────────────────────────────────────────────────────────────────

    def owner_cap(self, job: Job) -> int:
        """The owner's limit for *job*: one while its Conversation's turn runs."""
        if (
            job.conversation_id
            and self._status(job.workspace_id, job.conversation_id) == "running"
        ):
            return 1
        measured = None
        if _ROOM:
            try:
                measured = _ROOM[0](job.workspace_id, job.owner)
            except Exception:
                _logger.exception("work queue: owner room hook failed")
        return owner_limit(measured)

    def _status(self, ws: int, run_id: str) -> str:
        try:
            with workspace_scope(ws):
                return self._run_status(run_id)
        except Exception:
            _logger.warning("work queue: status of %s unreadable", run_id, exc_info=True)
            return ""

    # ── claim and run ─────────────────────────────────────────────────────────

    def _bind_loop(self) -> None:
        """Use the running loop (a test's loop differs per test; the engine has one)."""
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._claim_lock = asyncio.Lock()
            self._wake = asyncio.Event()

    async def pump(self) -> int:
        """Claim what may start now and start each handler; returns how many were claimed."""
        self._bind_loop()
        async with self._claim_lock:
            now = int(time.time())
            caps: dict[tuple[int, str, str | None], int] = {}

            def cap(job: Job) -> int:  # read once per owner and Conversation per claim
                key = (job.workspace_id, job.owner, job.conversation_id)
                if key not in caps:
                    caps[key] = self.owner_cap(job)
                return caps[key]

            claimed: list[Job] = await asyncio.to_thread(
                self.store.claim,
                now=now,
                kinds=list(_HANDLERS),
                global_limit=global_limit(),
                owner_cap=cap,
                claimer=self.claimer,
                lease_until=now + LEASE_SECONDS,
            )
            for job in claimed:
                task = asyncio.create_task(self._run(job), name=f"work-{job.kind}-{job.id[:8]}")
                key = (job.workspace_id, job.id)
                self._tasks[key] = task
                task.add_done_callback(lambda _t, key=key: self._tasks.pop(key, None))
            return len(claimed)

    async def _run(self, job: Job) -> None:
        handler = _HANDLERS.get(job.kind)
        try:
            with workspace_scope(job.workspace_id):
                if handler is None:
                    raise RuntimeError(f"no handler for {job.kind}")
                result = await handler(job)
                now = int(time.time())
                if isinstance(result, Defer):
                    payload = result.payload if result.payload is not None else job.payload
                    await asyncio.to_thread(self.store.requeue, job, int(result.at), payload, now)
                elif isinstance(result, str) and result:
                    if await asyncio.to_thread(self.store.bind, job, result, now):
                        self._bound[result] = (job.workspace_id, job.id)
                else:
                    await asyncio.to_thread(self.store.finish, job.workspace_id, job.id, DONE, now)
        except asyncio.CancelledError:
            # Shutdown mid-handler: the claim stays and returns to the queue when its lease ends.
            raise
        except Exception as exc:
            _logger.exception("work queue: %s job %s failed", job.kind, job.id)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.store.finish,
                    job.workspace_id,
                    job.id,
                    FAILED,
                    int(time.time()),
                    type(exc).__name__,
                )
        finally:
            self.kick()

    # ── run ends ───────────────────────────────────────────────────────────────

    def is_bound(self, run_id: str) -> bool:
        """Whether a job of this process is waiting on *run_id* (cheap; no database)."""
        return run_id in self._bound

    def run_ended(self, run_id: str, ok: bool = True) -> bool:
        """Blocking: the run's turn ended; finish the job bound to it and free its slot.

        :returns: ``True`` when a job was finished.
        """
        bound = self._bound.pop(run_id, None)
        if bound is None:
            return False
        ws, job_id = bound
        done = self.store.finish(ws, job_id, DONE if ok else FAILED, int(time.time()))
        self.kick()
        return bool(done)

    def release_run(self, run_id: str) -> bool:
        """Blocking: the run reconciler gave up on *run_id*; fail its job, freeing the slot."""
        released = self.run_ended(run_id, ok=False)
        if released:
            return True
        for job in self.store.running():
            if job.run_id == run_id:
                self.store.finish(job.workspace_id, job.id, FAILED, int(time.time()), "stale")
                self.kick()
                return True
        return False

    # ── reaper ─────────────────────────────────────────────────────────────────

    def reap(self, now: int | None = None) -> int:
        """Blocking: return dead claims, finish ended or stale runs, prune old rows.

        :returns: Jobs released (claims returned plus runs finished).
        """
        ts = int(time.time()) if now is None else now
        self.store.renew(self.claimer, list(self._tasks), ts + LEASE_SECONDS)
        released = self.store.expire_claims(ts)
        bound: dict[str, tuple[int, str]] = {}
        for job in self.store.running():
            assert job.run_id is not None
            age = ts - job.updated_at
            status = self._status(job.workspace_id, job.run_id)
            ended = status == "missing" or (
                status in ("idle", "failed") and age >= RUN_START_GRACE_SECONDS
            )
            if ended or age >= RUN_MAX_AGE_SECONDS:
                state = DONE if status == "idle" else FAILED
                error = None if ended else "stale"
                if self.store.finish(job.workspace_id, job.id, state, ts, error):
                    released += 1
                continue
            bound[job.run_id] = (job.workspace_id, job.id)
        self._bound = bound
        self.store.prune(ts - KEEP_FINISHED_SECONDS)
        if released:
            self.kick()
        return released

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the worker on the running loop."""
        self._bind_loop()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._loop_forever(), name="work-queue")

    async def _loop_forever(self) -> None:
        while True:
            try:
                if time.monotonic() - self._last_reap >= REAP_SECONDS:
                    self._last_reap = time.monotonic()
                    await asyncio.to_thread(self.reap)
                await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("work queue: worker tick failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_s)

    async def drain(self) -> None:
        """Run everything due now to its end (handlers return); for tests and shutdown paths."""
        self._bind_loop()
        for _ in range(100):
            claimed = await self.pump()
            running = [t for t in self._tasks.values() if not t.done()]
            if not claimed and not running:
                return
            if running:
                await asyncio.gather(*running, return_exceptions=True)

    async def shutdown(self, grace_s: float = 10.0) -> None:
        """Stop the worker; give running handlers *grace_s*, then cancel them."""
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        running = [t for t in self._tasks.values() if not t.done()]
        if running:
            _done, pending = await asyncio.wait(running, timeout=grace_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


# custom-lint: disable-next=workspace-scoped-cache -- the process's one worker
_DEFAULT: list[WorkQueue] = []


def default() -> WorkQueue:
    """This process's queue: the configured one, else an in-memory queue (no database)."""
    if not _DEFAULT:
        _DEFAULT.append(WorkQueue(MemoryWorkQueueStore()))
    return _DEFAULT[0]


def configure(queue: WorkQueue | None) -> WorkQueue | None:
    """Install *queue* as this process's queue (``None`` goes back to the in-memory default)."""
    _DEFAULT[:] = [queue] if queue is not None else []
    return queue


def submit(
    owner: str,
    kind: str,
    key: str,
    payload: dict[str, Any] | None = None,
    not_before: float | None = None,
    *,
    conversation_id: str | None = None,
    owner_limit: int | None = None,
) -> str:
    """Blocking: queue a background model run; idempotent on ``(kind, key)`` while it is open.

    :param owner: Who the run is for (``"local"`` in single-user mode); limits count per owner.
    :param kind: The handler that runs it (:func:`register_handler`).
    :param key: Idempotency key within *kind*: while a job with it is queued or running, the same
        submit returns that job; once it finished, a submit queues it again.
    :param payload: JSON object the handler reads.
    :param not_before: Epoch seconds before which it does not start (default now).
    :param conversation_id: The Conversation whose running turn lowers the owner's limit to one.
    :param owner_limit: Start only while fewer than this many of the owner's jobs are in flight.
    :returns: The job id.
    """
    return default().submit(
        owner,
        kind,
        key,
        payload,
        not_before,
        conversation_id=conversation_id,
        owner_limit=owner_limit,
    )


def with_payload(job: Job, **changes: Any) -> dict[str, Any]:
    """*job*'s payload with *changes* applied (for a :class:`Defer`)."""
    return {**job.payload, **changes}


__all__ = [
    "DEFAULT_GLOBAL_LIMIT",
    "DEFAULT_OWNER_LIMIT",
    "Defer",
    "Handler",
    "Job",
    "WorkQueue",
    "configure",
    "default",
    "global_limit",
    "owner_limit",
    "register_handler",
    "set_owner_room",
    "submit",
    "with_payload",
]
