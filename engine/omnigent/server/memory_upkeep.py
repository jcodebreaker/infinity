"""Server-side scheduling for the memory upkeep job (Phase 3).

Two triggers (``docs/context.md``): applies only to ``superside-chat`` sessions
(native-CLI ``rollover`` sessions keep today's behaviour unchanged).

- :class:`MemoryUpkeepCoordinator` — a work-queue job per user (ADR 0038, kind
  ``memory_upkeep``, at most one waiting or running per user), queued right after a
  ``superside-chat`` session's
  compaction item is recorded (``routes/sessions/routes_events.py``, via
  :mod:`omnigent.superchat.memory`).
- :class:`MemoryUpkeepScheduler` — an hourly sweep (mirrors
  :class:`~omnigent.server.managed_sandbox_reaper.ManagedSandboxReaper`)
  that enqueues a run for every recently-active user.

Both ultimately call :func:`omnigent.memory.upkeep.run_upkeep_once`, which
holds the real per-user lease (a ``running`` row); the queue's key folds a second
request while one is open, and its limits keep upkeep from running in rushes.

Beside them, :class:`SessionFolds` runs the fold (ADR 0031): after a superside-chat turn
completes, it checks in the background whether the session's past is due to be folded into its
checkpoint and folds it on the owner's Light model
(:func:`omnigent.superchat.rollover.fold_session`). A stored fold then schedules upkeep, as a
compaction always did.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from omnigent.memory.service import MemoryService
from omnigent.memory.upkeep import (
    GATE_MIN_USER_MESSAGES,
    UpkeepLLMCaller,
    describe_upkeep_run,
    resolve_upkeep_max_concurrency,
    resolve_upkeep_sweep_interval_seconds,
    run_upkeep_once,
)
from omnigent.server import work_queue
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.memory_upkeep_store import MemoryUpkeepStore

_logger = logging.getLogger(__name__)


#: How standing memory is kept, returned with ``memory_search`` so the Assistant explains its
#: memory from the code's rules, not from guesses. Keep in step with ``MEMORY_POLICY``.
MEMORY_HOW_IT_WORKS = (
    "Standing memory is kept by memory upkeep. Every hour, and after a conversation is "
    "condensed, it reads the person's new messages (not your replies, background work or tool "
    f"output; with fewer than {GATE_MIN_USER_MESSAGES} new messages it waits for the next run), "
    "shows the person's messages from the last two weeks beside them, and keeps lasting facts "
    "about the person: who they are, what they work on, recurring topics and audiences, and how "
    "they like work done. Each memory carries the person's exact words and where they said them. "
    "Something said directly is kept at once; a pattern is kept as noticed (inferred) and joins "
    "standing memory once it has shown up about three times. One-off instructions for a single "
    "task, choices you made, questions about you, and secrets are never kept. "
    "Separately, you save what the person explicitly asks you to keep during a turn "
    "(memory_remember), the daily note records each day (its quiet pass may save lasting "
    "facts by the same rules), and the conversation's working list tracks open work. The person "
    "can ask you to remember or forget something, and can see, edit or forget each memory, with "
    "its source quote, in the Memory tab of the side panel."
)


def upkeep_status(app_state: Any, owner: str, *, runs: int = 1) -> dict[str, Any] | None:
    """The upkeep job as this person sees it: cadence, next run and the last runs' outcomes.

    Read by the Automations System row and returned with ``memory_search`` so the Assistant
    can say what its memory upkeep actually did. ``None`` when upkeep is not running here.
    """
    coordinator = getattr(app_state, "memory_upkeep_coordinator", None)
    if coordinator is None:
        return None
    scheduler = getattr(app_state, "memory_upkeep_scheduler", None)
    interval = int(scheduler.sweep_interval_s) if scheduler is not None else None
    last_sweep = getattr(scheduler, "last_sweep_at", None)
    recent = coordinator.upkeep_store.list_runs(owner, limit=runs)
    return {
        "interval_seconds": interval,
        "next_run_at": int(last_sweep + interval) if last_sweep and interval else None,
        "runs": [
            {
                "at": run.finished_at or run.started_at,
                "state": run.state,
                "outcome": describe_upkeep_run(run),
            }
            for run in recent
        ],
    }


#: The work-queue kind of an upkeep run.
UPKEEP_JOB_KIND = "memory_upkeep"


class MemoryUpkeepCoordinator:
    """Queue at most one open upkeep run per user in the work queue, fire-and-forget."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        memory_service: MemoryService,
        upkeep_store: MemoryUpkeepStore,
        llm_caller_factory: Callable[[], UpkeepLLMCaller | None],
        max_concurrency: int | None = None,
        llm_caller_resolver: Callable[[str], Awaitable[UpkeepLLMCaller | None]] | None = None,
    ) -> None:
        resolved_max_concurrency = (
            max_concurrency if max_concurrency is not None else resolve_upkeep_max_concurrency()
        )
        if resolved_max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._conversation_store = conversation_store
        self._memory_service = memory_service
        self._upkeep_store = upkeep_store
        self._llm_caller_factory = llm_caller_factory
        # Per-owner resolution (the owner's own connection); wins over the factory when set.
        self._llm_caller_resolver = llm_caller_resolver
        # Upkeep's own cap (``OMNIGENT_MEMORY_UPKEEP_MAX_CONCURRENCY``) inside the queue's limits.
        self._slots = asyncio.Semaphore(resolved_max_concurrency)
        # Submits on their way to the queue (each is one blocking insert off the loop).
        self._pending: set[asyncio.Task[Any]] = set()
        work_queue.register_handler(UPKEEP_JOB_KIND, self._run_job)

    @property
    def upkeep_store(self) -> MemoryUpkeepStore:
        """The run store (read by the read-only System jobs view)."""
        return self._upkeep_store

    def schedule(self, user_id: str) -> None:
        """Queue a run for *user_id*; one already waiting or running absorbs it."""
        task = asyncio.create_task(
            asyncio.to_thread(work_queue.submit, user_id, UPKEEP_JOB_KIND, user_id),
            name=f"memory-upkeep-{user_id}",
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _run_job(self, job: work_queue.Job) -> None:
        """The ``memory_upkeep`` job: one upkeep run for the job's owner, inside the engine."""
        await self._run(job.owner)

    async def _run(self, user_id: str) -> None:
        try:
            async with self._slots:
                llm_caller = (
                    await self._llm_caller_resolver(user_id)
                    if self._llm_caller_resolver is not None
                    else self._llm_caller_factory()
                )
                await run_upkeep_once(
                    user_id=user_id,
                    conversation_store=self._conversation_store,
                    memory=self._memory_service,
                    upkeep_store=self._upkeep_store,
                    llm_caller=llm_caller,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # background job must never crash the caller
            _logger.warning("memory upkeep run failed user=%s", user_id, exc_info=True)

    async def wait_for_idle(self) -> None:
        """Wait for currently scheduled runs to finish; used by focused tests."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending))
        await work_queue.default().drain()

    async def shutdown(self) -> None:
        """Cancel submits still on their way (queued runs stay queued for the next start)."""
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class MemoryUpkeepScheduler:
    """Hourly sweep: enqueue every recently-active user's upkeep run.

    "Recently active" is deliberately cheap and approximate — a session
    touched since the last sweep, scanned across all users. A false
    positive costs one gated (and watermark-preserving) skip; a user
    missed this sweep is caught by the next one or by the compaction
    trigger.
    """

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        coordinator: MemoryUpkeepCoordinator,
        sweep_interval_s: int | None = None,
        scan_limit: int = 200,
    ) -> None:
        self._conversation_store = conversation_store
        self._coordinator = coordinator
        self._sweep_interval_s = (
            sweep_interval_s
            if sweep_interval_s is not None
            else resolve_upkeep_sweep_interval_seconds()
        )
        self._scan_limit = scan_limit
        self._task: asyncio.Task[None] | None = None
        self.last_sweep_at: int | None = None

    @property
    def sweep_interval_s(self) -> int:
        """Seconds between sweeps."""
        return self._sweep_interval_s

    async def start(self) -> None:
        """Start this server process's hourly sweep loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="memory-upkeep-scheduler")

    async def shutdown(self) -> None:
        """Stop the sweep loop and wait for cancellation to settle."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def sweep_once(self) -> int:
        """Enqueue one run per recently-active user; return how many."""
        user_ids = await asyncio.to_thread(self._recently_active_user_ids)
        for user_id in user_ids:
            self._coordinator.schedule(user_id)
        return len(user_ids)

    def _recently_active_user_ids(self) -> list[str]:
        page = self._conversation_store.list_conversations(
            kind=None,
            limit=self._scan_limit,
            order="desc",
            sort_by="updated_at",
        )
        owners: set[str] = set()
        for conv in page.data:
            owner = self._conversation_store.get_session_owner(conv.id)
            if owner:
                owners.add(owner)
        return sorted(owners)

    async def _run(self) -> None:
        while True:
            try:
                enqueued = await self.sweep_once()
                self.last_sweep_at = int(time.time())
                if enqueued:
                    _logger.info("Memory upkeep sweep enqueued %s user(s)", enqueued)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Memory upkeep sweep failed; retrying later")
            await asyncio.sleep(self._sweep_interval_s)


class SessionFolds:
    """Fold a superside-chat session's past after its turns, in the background (ADR 0031).

    At most one check or fold per session at a time: a turn that completes while one runs adds
    nothing, and the next turn checks again. Nothing here is awaited by a turn.
    """

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        coordinator: MemoryUpkeepCoordinator | None = None,
    ) -> None:
        self._conversation_store = conversation_store
        self._coordinator = coordinator
        self._running: dict[str, asyncio.Task[None]] = {}

    def after_turn(self, session_id: str, usage: dict[str, Any] | None) -> None:
        """Schedule the fold check for *session_id*, whose turn just completed with *usage*."""
        if session_id in self._running:
            return
        usage = usage or {}
        tokens = usage.get("context_tokens")
        model = usage.get("model")
        task = asyncio.create_task(
            self._run(
                session_id,
                tokens if isinstance(tokens, int) else None,
                model if isinstance(model, str) and model else None,
            ),
            name=f"fold-{session_id}",
        )
        self._running[session_id] = task
        task.add_done_callback(lambda _t, _sid=session_id: self._running.pop(_sid, None))

    async def _run(self, session_id: str, tokens: int | None, model: str | None) -> None:
        from omnigent.superchat.rollover import fold_session

        try:
            stored = await fold_session(
                self._conversation_store,
                session_id,
                context_tokens=tokens,
                session_model=model,
            )
            if stored and self._coordinator is not None:
                owner = await asyncio.to_thread(
                    self._conversation_store.get_session_owner, session_id
                )
                if owner:
                    self._coordinator.schedule(owner)
        except asyncio.CancelledError:
            raise
        except Exception:  # background job must never crash the relay
            _logger.warning("fold failed session=%s", session_id, exc_info=True)

    async def shutdown(self) -> None:
        """Cancel and drain running folds during server shutdown."""
        running = tuple(self._running.values())
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)


_folds: SessionFolds | None = None


def register_session_folds(folds: SessionFolds | None) -> None:
    """Install this process's :class:`SessionFolds` (``None`` turns the fold off)."""
    global _folds
    _folds = folds


def schedule_fold(session_id: str, usage: dict[str, Any] | None) -> None:
    """After a turn's ``response.completed``: schedule its session's fold check, if installed."""
    if _folds is not None:
        _folds.after_turn(session_id, usage)
