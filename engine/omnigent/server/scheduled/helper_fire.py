"""The parent-bound (Helper) half of the scheduled fire path.

A parent-bound task fires as a Helper session under its Super Chat. Each fire is a job in the
work queue (:mod:`omnigent.server.work_queue`, kind ``scheduled_fire``, ADR 0038): the queue
holds it until the owner has room (their limit, one while their own turn runs, and the limit
across everyone), and its handler gates it on the owner's proactivity and quiet hours, defers it
(stored, so a restart keeps it), retries it once after a failed launch, and launches it on the
parent's own runner. The job holds its slot until the Helper's turn ends. The plumbing common to
every fire (``FireDeps``, run records, grants) stays in :mod:`omnigent.server.scheduled.fire`,
which imports this module for its own use; this module reads it through ``fire`` at call time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.context.labels import (
    ADHOC_HELPER_LABEL_KEY,
    SCHEDULED_HELPER_LABEL_KEY,
    SUBAGENT_DISPATCH_ID_LABEL_KEY,
    UNATTENDED_LABEL_KEY,
    inheritable_context_labels,
)
from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation, OwnerPreferences, ScheduledTask
from omnigent.errors import OmnigentError
from omnigent.server import work_queue
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import host_owner_scope
from omnigent.server.scheduled.rrule import RRuleValidationError, get_next_fire_time
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store import NameAlreadyExistsError
from omnigent.superchat.activity.titles import fire_title_candidates
from omnigent.superchat.subagents import (
    CONVERSATION_MODEL,
    resolve_scheduled_helper,
    scheduled_helper_type,
)

# The same logger as ``fire`` (shared name, so existing log filters keep matching).
_logger = logging.getLogger("omnigent.server.scheduled.fire")

if TYPE_CHECKING:
    from omnigent.server.scheduled.fire import FireDeps, LaunchDispatch, _FireDispatch


#: The work-queue kind of a parent-bound fire.
FIRE_KIND = "scheduled_fire"


#: The ``scheduled_fire`` handler of the last wiring, for kinds that queue their own jobs.
_FIRE_HANDLER: list[work_queue.Handler] = []


def _register_fire_handler(deps: FireDeps, fire_dispatch: _FireDispatch) -> None:
    """Run ``scheduled_fire`` jobs with these server deps (the last wiring wins)."""

    async def handle(job: work_queue.Job) -> str | work_queue.Defer | None:
        payload = job.payload
        task = await asyncio.to_thread(deps.scheduled_task_store.get, payload.get("task_id"))
        if task is None or task.state == "deleted":
            _logger.info("scheduled fire: task %s no longer exists - skipping", payload)
            return None
        if payload.get("require_active", True) and task.state != "active":
            _logger.info("scheduled fire: task %s is %s - skipping", task.id, task.state)
            return None
        with (
            account_authority_scope(task.user_id, task.account_generation),
            host_owner_scope(task.user_id, task.account_generation),
        ):
            return await _run_parent_bound_fire(
                deps, task, fire_dispatch, int(payload["scheduled_at"]), payload=payload
            )

    _FIRE_HANDLER[:] = [handle]
    work_queue.register_handler(FIRE_KIND, handle)


def fire_job_payload(task: ScheduledTask, scheduled_at: int, *, require_active: bool) -> dict:
    """The payload a fire job of *task* carries (:func:`run_fire_job` reads it), for a kind that
    queues its own job (e.g. the Lookout's ``lookout_wake``) instead of ``scheduled_fire``."""
    return {
        "task_id": task.id,
        "scheduled_at": int(scheduled_at),
        "attempt": 1,
        "require_active": require_active,
        "drop_after": _drop_after(task, scheduled_at),
    }


async def run_fire_job(job: work_queue.Job) -> str | work_queue.Defer | None:
    """Run *job* (payload from :func:`fire_job_payload`) through the parent-bound fire path:
    the same gates, Helper launch and run record as a ``scheduled_fire`` job.

    :raises RuntimeError: Before the fire path is wired (the job fails; its owner releases).
    """
    if not _FIRE_HANDLER:
        raise RuntimeError("the scheduled fire path is not wired")
    return await _FIRE_HANDLER[0](job)


def _drop_after(task: ScheduledTask, from_s: float) -> float | None:
    """When a fire that waits for the owner's other runs is given up (``None``: never).

    Queued kinds (and engine runs, which hold no Helper) wait as long as it takes; any other kind
    waits at most ``_MAX_OWNER_BUSY_DEFERRALS`` x ``_OWNER_BUSY_DEFER_S`` past *from_s*.
    """
    if task.kind in _QUEUED_KINDS or (task.kind or "") in _ENGINE_RUNS:
        return None
    return from_s + fire._MAX_OWNER_BUSY_DEFERRALS * fire._OWNER_BUSY_DEFER_S


def submit_fire(
    deps: FireDeps,
    task: ScheduledTask,
    fire_dispatch: _FireDispatch,
    scheduled_at: int,
    *,
    require_active: bool = True,
    attempt: int = 1,
    key: str | None = None,
    not_before: float | None = None,
) -> bool:
    """Blocking: queue one fire of a parent-bound task in the work queue.

    Keyed by the occurrence (``<task id>:<scheduled_at>``) unless *key* is given, so a repeated
    submit of the same fire is folded into the queued one.

    :returns: ``True`` when a job was queued, ``False`` when the same one was already open.
    """
    _register_fire_handler(deps, fire_dispatch)
    exclusive = _drop_after(task, 0) is not None
    start = not_before if not_before is not None else scheduled_at
    _job_id, created = work_queue.default().put(
        task.user_id or RESERVED_USER_LOCAL,
        FIRE_KIND,
        key or f"{task.id}:{scheduled_at}",
        {
            "task_id": task.id,
            "scheduled_at": scheduled_at,
            "attempt": attempt,
            "require_active": require_active,
            "drop_after": _drop_after(task, start),
        },
        not_before,
        conversation_id=task.parent_session_id,
        # Any other kind starts only while none of the owner's runs is in flight.
        owner_limit=1 if exclusive else None,
    )
    return created


def build_retry(
    deps: FireDeps,
    *,
    launch_dispatch: LaunchDispatch | None = None,
) -> Callable[[int, str, int], bool]:
    """Build the trigger that schedules the single retry of a failed fire.

    Used by the run reconciler when it force-fails a stale parent-bound run. Blocking (it reads
    the task and writes the queue).

    :returns: ``retry(workspace_id, scheduled_task_id, failed_attempt) -> bool``;
        ``True`` when a retry was queued (only after a first attempt, at most one open per task).
    """
    fire_dispatch = fire._build_fire_dispatch(deps, launch_dispatch)

    def retry(workspace_id: int, scheduled_task_id: str, failed_attempt: int) -> bool:
        if failed_attempt >= fire._MAX_ATTEMPTS:
            return False
        with workspace_scope(workspace_id):
            task = deps.scheduled_task_store.get(scheduled_task_id)
            if task is None or task.parent_session_id is None:
                return False
            now = time.time()
            return submit_fire(
                deps,
                task,
                fire_dispatch,
                int(now),
                attempt=failed_attempt + 1,
                key=f"{task.id}:retry",
                not_before=now + fire._RETRY_BACKOFF_S,
            )

    return retry


#: ``task kind -> hook(task)``: epoch seconds the task's run should wait until, or ``None``.
_FIRE_DEFERRALS: dict[str, Callable[[ScheduledTask], float | None]] = {}


def register_fire_deferral(kind: str, hook: Callable[[ScheduledTask], float | None]) -> None:
    """Make a parent-bound task of ``kind`` wait while ``hook(task)`` is in the future."""
    _FIRE_DEFERRALS[kind] = hook


#: ``task kind -> hook(task)``: whether the person stopped a task of that kind for now (a reach
#: rule that names the morning brief, ADR 0038). A scheduled occurrence is then skipped, never
#: held, so a pause over several days never delivers a pile of runs when it ends; run-now still
#: runs.
_FIRE_SKIPS: dict[str, Callable[[ScheduledTask], bool]] = {}


def register_fire_skip(kind: str, hook: Callable[[ScheduledTask], bool]) -> None:
    """Skip a scheduled (not run-now) occurrence of a parent-bound task of ``kind`` while
    ``hook(task)`` is true."""
    _FIRE_SKIPS[kind] = hook


#: Kinds whose due runs are never dropped for "busy": they wait in the work queue and run up to
#: the owner's limit at once (one at a time while the parent Conversation's own turn runs).
_QUEUED_KINDS = frozenset({"live_artifact", "automation", "daily_brief", "lookout_wake"})


@dataclass(frozen=True)
class _OwnerRef:
    """The two task fields a room hook reads."""

    workspace_id: int
    user_id: str


def register_room(hook: Callable[[ScheduledTask], int | None]) -> None:
    """Let ``hook(task)`` size the owner's limit in the work queue from their host's load."""
    work_queue.set_owner_room(lambda ws, owner: hook(_OwnerRef(ws, owner)))  # type: ignore[arg-type]


#: ``task kind -> hook(task, failure)`` called once per finished run (``failure`` is the plain
#: reason, ``None`` on success); may return a note to post in the parent Conversation.
_RUN_OUTCOMES: dict[str, Callable[[ScheduledTask, str | None], str | None]] = {}


def register_run_outcome(
    kind: str, hook: Callable[[ScheduledTask, str | None], str | None]
) -> None:
    """Tell ``hook`` how each run of a ``kind`` task ended; a returned note is posted."""
    _RUN_OUTCOMES[kind] = hook


def report_run_outcome(conversation_store: Any, task: ScheduledTask, failure: str | None) -> bool:
    """Blocking: pass a finished run to its kind's hook and post the note it returns.

    :returns: ``True`` when a note was posted.
    """
    hook = _RUN_OUTCOMES.get(task.kind or "")
    if hook is None or task.parent_session_id is None:
        return False
    try:
        note = hook(task, failure)
        if note:
            _append_note(conversation_store, task.parent_session_id, note)
    except Exception:
        _logger.exception("scheduled fire: run outcome hook failed for task %s", task.id)
        return False
    return bool(note)


#: ``task kind -> hook(task, run conversation id, status)``: kinds whose Result is posted by
#: code (ADR 0038, "One run, posted by code"). Their Helper is created without the scheduled label
#: (it carries the ad-hoc one with the task id), so the runner never wakes the parent with it; the
#: hook hears every run row the fire path writes (``running``, ``skipped``, ``failed``) and every
#: terminal edge (``succeeded``, ``failed``), and posts or releases itself.
_RUN_RESULTS: dict[str, Callable[[ScheduledTask, str | None, str], None]] = {}


def register_run_result(kind: str, hook: Callable[[ScheduledTask, str | None, str], None]) -> None:
    """Post the Result of each ``kind`` run by code: ``hook(task, run_conversation_id, status)``
    is told of every run row and terminal edge (blocking; it must not block for long)."""
    _RUN_RESULTS[kind] = hook


def posts_by_code(kind: str | None) -> bool:
    """Whether a ``kind`` run's Result is posted by code instead of waking its parent."""
    return bool(kind) and kind in _RUN_RESULTS


def report_run_result(task: ScheduledTask, conversation_id: str | None, status: str) -> None:
    """Blocking: tell a code-posted kind's hook how its run moved; never raises."""
    hook = _RUN_RESULTS.get(task.kind or "")
    if hook is None:
        return
    try:
        hook(task, conversation_id, status)
    except Exception:
        _logger.exception("scheduled fire: run result hook failed for task %s", task.id)


@dataclass(frozen=True)
class EngineRunOutcome:
    """How an engine run ended, recorded as the scheduled run's row.

    :param status: ``succeeded``, ``skipped`` or ``failed``.
    :param code: Short reason for a skipped or failed run, e.g. ``topics_unavailable``.
    :param detail: Plain detail kept as the run's ``error`` text, if any.
    """

    status: str = "succeeded"
    code: str | None = None
    detail: str | None = None


#: ``task kind -> run(task)``: kinds whose fire runs engine code instead of starting a Helper.
_ENGINE_RUNS: dict[str, Callable[[ScheduledTask], Awaitable[EngineRunOutcome]]] = {}


def register_engine_run(
    kind: str, run: Callable[[ScheduledTask], Awaitable[EngineRunOutcome]]
) -> None:
    """Make a parent-bound task of ``kind`` run ``run(task)`` in the engine, past the same gates.

    Proactivity, quiet hours and a registered deferral apply as for a Helper fire. The run starts
    no Helper and wakes no host, so it neither waits for nor holds the owner's in-flight slot; it
    is recorded as one finished run, and a failure is not retried (the next occurrence runs).
    """
    _ENGINE_RUNS[kind] = run


async def _run_engine(
    deps: FireDeps,
    task: ScheduledTask,
    run: Callable[[ScheduledTask], Awaitable[EngineRunOutcome]],
    scheduled_at: int,
    *,
    attempt: int,
) -> None:
    """Run an engine-run kind's code and record how it ended."""
    try:
        outcome = await run(task)
    except Exception as exc:
        _logger.exception("scheduled fire: engine run of task %s failed", task.id)
        outcome = EngineRunOutcome("failed", "engine_run_failed", type(exc).__name__)
    await fire._record_run(
        deps,
        task,
        None,
        scheduled_at,
        status=outcome.status,
        error=outcome.detail,
        error_code=outcome.code,
        attempt=attempt,
    )


#: ``task kind -> hook(task)``: the whole brief a run of that kind starts with, built when it fires
#: (blocking; it may read stores). ``None`` falls back to the task's own prompt.
_RUN_BRIEFS: dict[str, Callable[[ScheduledTask], str | None]] = {}


def register_run_brief(kind: str, hook: Callable[[ScheduledTask], str | None]) -> None:
    """Build the opening message of each run of a ``kind`` task with ``hook(task)``."""
    _RUN_BRIEFS[kind] = hook


def _quiet_window_end(prefs: OwnerPreferences, now: float) -> float | None:
    """Epoch seconds the owner's quiet window ends, or ``None`` when ``now`` is outside it.

    The window is local ``quiet_start``-``quiet_end`` in ``prefs.timezone`` and may
    cross midnight (e.g. 22:00-07:00). Equal bounds mean no window.
    """
    if prefs.quiet_start is None or prefs.quiet_end is None:
        return None
    try:
        tz = ZoneInfo(prefs.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    start = dtime.fromisoformat(prefs.quiet_start)
    end = dtime.fromisoformat(prefs.quiet_end)
    if start == end:
        return None
    local = datetime.fromtimestamp(now, tz)
    t = local.time()
    if start < end:
        inside, end_date = start <= t < end, local.date()
    else:
        inside = t >= start or t < end
        end_date = local.date() if t < end else local.date() + timedelta(days=1)
    if not inside:
        return None
    return datetime.combine(end_date, end, tzinfo=tz).timestamp()


async def _run_parent_bound_fire(
    deps: FireDeps,
    task: ScheduledTask,
    fire_dispatch: _FireDispatch,
    scheduled_at: int,
    *,
    payload: dict[str, Any],
) -> str | work_queue.Defer | None:
    """The ``scheduled_fire`` job's body: gate per owner, then start the task's Helper.

    The work queue only claims the job while the owner has room: up to their limit (one while
    the parent Conversation's turn runs; a kind outside :data:`_QUEUED_KINDS` only while none of
    their runs is in flight). Gating (recorded as a ``skipped`` run with an ``error_code``):

    * proactivity ``off`` -> ``proactivity_off`` (skipped).
    * a registered per-kind skip hook says the person stopped it (a scheduled occurrence, not
      run-now) -> ``paused_by_person`` (skipped).
    * inside the owner's quiet hours -> ``quiet_hours_deferred``; the job waits for the end of
      the window.
    * a registered per-kind hook asks to wait -> ``task_deferred``; the job waits until then.
    * a kind registered with :func:`register_engine_run` then runs its engine code (no Helper)
      and records the outcome it returns.
    * a kind outside :data:`_QUEUED_KINDS` that waited past its ``drop_after`` for the owner's
      other runs -> ``owner_busy`` (skipped).

    A failed launch records a ``failed`` run and waits 10 minutes for one retry (``attempt`` 2),
    unless the owner's proactivity is ``low``.

    :returns: The Helper's session id (the job holds its slot until its turn ends), a
        :class:`~omnigent.server.work_queue.Defer`, or ``None`` when nothing is left running.
    """
    attempt = int(payload.get("attempt", 1))
    owner = task.user_id or RESERVED_USER_LOCAL
    prefs = await asyncio.to_thread(deps.scheduled_task_store.get_owner_preferences, owner)
    prefs = prefs or OwnerPreferences(user_id=owner)

    async def skip(code: str, error: str) -> None:
        await fire._record_run(
            deps,
            task,
            None,
            scheduled_at,
            status="skipped",
            error=error,
            error_code=code,
            attempt=attempt,
        )

    def wait_until(at: float) -> work_queue.Defer:
        drop = _drop_after(task, at)
        return work_queue.Defer(at, {**payload, "drop_after": drop})

    if deps.objective_store is not None:
        objective = await asyncio.to_thread(deps.objective_store.get_by_scheduled_task_id, task.id)
        if objective is not None and objective.status != "active":
            await skip("objective_inactive", f"objective is {objective.status}")
            return None

    if prefs.proactivity == "off":
        await skip("proactivity_off", "proactivity is off for the task owner")
        return None

    skip_hook = _FIRE_SKIPS.get(task.kind or "")
    if (
        skip_hook is not None
        and payload.get("require_active", True)
        and await asyncio.to_thread(skip_hook, task)
    ):
        await skip("paused_by_person", "the person paused it")
        return None

    now = time.time()
    quiet_end = _quiet_window_end(prefs, now)
    if quiet_end is not None:
        await skip(
            "quiet_hours_deferred",
            "inside the owner's quiet hours; re-fire at "
            f"{datetime.fromtimestamp(quiet_end).astimezone().isoformat()}",
        )
        return wait_until(quiet_end)

    hook = _FIRE_DEFERRALS.get(task.kind or "")
    hold = await asyncio.to_thread(hook, task) if hook is not None else None
    if hold is not None and hold > now:
        await skip("task_deferred", "the task asked to wait")
        return wait_until(hold)

    engine_run = _ENGINE_RUNS.get(task.kind or "")
    if engine_run is not None:
        await _run_engine(deps, task, engine_run, scheduled_at, attempt=attempt)
        return None

    drop_after = payload.get("drop_after")
    if drop_after is not None and now > float(drop_after):
        await skip("owner_busy", "another proactive run is in flight")
        return None

    return await _launch_helper(
        deps,
        task,
        fire_dispatch,
        scheduled_at,
        attempt=attempt,
        retry=prefs.proactivity != "low",
        payload=payload,
    )


_DEFAULT_FAILURE_REASON = "it could not be started"


def _sentence(reason: str) -> str:
    """*reason* as a capitalised sentence."""
    return reason[:1].upper() + reason[1:] + ("" if reason.endswith(".") else ".")


async def _create_failed_helper_session(
    deps: FireDeps, task: ScheduledTask, scheduled_at: int
) -> str | None:
    """Create the Helper session a run that never started reports its failure on, if possible."""
    try:
        conv = await _create_helper_session(deps, task, scheduled_at)
        await fire._grant_owner(deps, task, conv.id)
        return conv.id
    except Exception:
        _logger.exception("scheduled fire: could not record a failed Helper for task %s", task.id)
        return None


async def _mark_helper_failed(deps: FireDeps, conv_id: str, message: str) -> None:
    """Make a Helper that never ran read as failed, with *message* as its reason."""
    from omnigent.entities import ErrorData, NewConversationItem
    from omnigent.server.routes._sessions.helpers import _publish_external_conversation_item

    try:
        persisted = await asyncio.to_thread(
            deps.conversation_store.append,
            conv_id,
            [
                NewConversationItem(
                    type="error",
                    response_id=fire._new_id(),
                    data=ErrorData(
                        source="execution", code="scheduled_run_failed", message=message
                    ),
                )
            ],
        )
        await asyncio.to_thread(deps.conversation_store.set_session_live_status, conv_id, "failed")
        if persisted:
            _publish_external_conversation_item(conv_id, persisted[0])
    except Exception:
        _logger.exception("scheduled fire: could not mark helper %s failed", conv_id)


async def _post_failure_note(deps: FireDeps, task: ScheduledTask, reason: str) -> None:
    """Tell the person, in the parent Conversation, that a scheduled run could not run.

    A calm system item rather than a Result wake: the wake would need the parent's runner,
    which is the thing that was down.
    """
    assert task.parent_session_id is not None
    again = "I'll try again at the next scheduled time."
    try:
        nxt = get_next_fire_time(
            task.rrule,
            datetime.now(ZoneInfo(task.timezone)),
            ZoneInfo(task.timezone),
            datetime.fromtimestamp(task.anchor_epoch, tz=ZoneInfo("UTC")),
        )
    except (ValueError, ZoneInfoNotFoundError, RRuleValidationError):
        nxt = None
    if task.rrule and nxt is None:
        again = "It has no more scheduled runs."
    message = f"Your {task.name} couldn't run: {reason}. {again}"
    try:
        await asyncio.to_thread(
            _append_note, deps.conversation_store, task.parent_session_id, message
        )
    except Exception:
        _logger.exception("scheduled fire: could not post the failure note for task %s", task.id)


def _append_note(conversation_store: Any, parent_session_id: str, message: str) -> None:
    """Blocking: put a calm system item in the parent Conversation and publish it."""
    from omnigent.entities import ErrorData, NewConversationItem
    from omnigent.server.routes._sessions.helpers import _publish_external_conversation_item

    persisted = conversation_store.append(
        parent_session_id,
        [
            NewConversationItem(
                type="error",
                response_id=fire._new_id(),
                data=ErrorData(
                    source="execution",
                    code="scheduled_run_failed",
                    message=message,
                    level="info",
                ),
            )
        ],
    )
    if persisted:
        _publish_external_conversation_item(parent_session_id, persisted[0])


# How long a scheduled Helper run waits for its parent's host to wake and its runner connect.
_PARENT_WAKE_TIMEOUT_S = 120.0


async def _wake_parent_runner(deps: FireDeps, parent: Conversation) -> Conversation:
    """Bring the parent's runner online, waking its sleeping managed host first.

    Reuses the path a user message takes (:func:`ensure_runner_connected`: resume or relaunch the
    managed sandbox, launch a runner on it, wait for the tunnel), so a scheduled run needs no
    separate launch logic. A parent whose runner is already connected returns at once.

    :returns: The parent row re-read after the wake (its runner id may have been rebound).
    :raises RuntimeError: When the host could not be brought online.
    """
    from omnigent.server.routes._sessions.orchestration import ensure_runner_connected

    if deps.app_state is None:  # embedders without server wiring have nothing to wake
        return parent
    try:
        client, woken = await asyncio.wait_for(
            ensure_runner_connected(
                session_id=parent.id,
                conv=parent,
                app_state=deps.app_state,
                conversation_store=deps.conversation_store,
                runner_router=deps.runner_router,
            ),
            timeout=_PARENT_WAKE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise RuntimeError("your machine didn't start in time") from exc
    except OmnigentError as exc:
        raise RuntimeError(f"your machine didn't start: {exc.message}") from exc
    if client is None:
        raise RuntimeError("your machine didn't start: the parent's runner is not connected")
    return woken


async def _launch_helper(
    deps: FireDeps,
    task: ScheduledTask,
    fire_dispatch: _FireDispatch,
    scheduled_at: int,
    *,
    attempt: int,
    retry: bool,
    payload: dict[str, Any] | None = None,
) -> str | work_queue.Defer | None:
    """Create the Helper child of the task's parent, dispatch its prompt, record the run.

    :returns: The Helper's session id once its prompt is out; a ``Defer`` for the one retry
        after a retryable failure; ``None`` after a final failure.
    """
    conv_id: str | None = None

    async def fail(
        error: str, code: str, *, retryable: bool, reason: str = _DEFAULT_FAILURE_REASON
    ) -> work_queue.Defer | None:
        nonlocal conv_id
        note = ""
        retry_armed = retryable and retry and attempt < fire._MAX_ATTEMPTS
        if retry_armed:
            note = f"; retry scheduled in {fire._RETRY_BACKOFF_S}s"
        _logger.warning("scheduled fire: task %s helper fire failed: %s%s", task.id, error, note)
        # The run must read as failed in Activity: mark its Helper failed (creating one on the
        # final attempt when the failure came before any was made), with the reason as its outcome.
        if conv_id is None and not retry_armed:
            conv_id = await _create_failed_helper_session(deps, task, scheduled_at)
        if conv_id is not None:
            await _mark_helper_failed(deps, conv_id, _sentence(reason))
        await fire._record_run(
            deps,
            task,
            conv_id,
            scheduled_at,
            status="failed",
            error=error + note,
            error_code=code,
            attempt=attempt,
        )
        if retry_armed:
            return work_queue.Defer(
                time.time() + fire._RETRY_BACKOFF_S, {**(payload or {}), "attempt": attempt + 1}
            )
        noted = await asyncio.to_thread(report_run_outcome, deps.conversation_store, task, reason)
        if not noted:
            await _post_failure_note(deps, task, reason)
        return None

    assert task.parent_session_id is not None and task.agent_type is not None
    parent = await asyncio.to_thread(
        deps.conversation_store.get_conversation, task.parent_session_id
    )
    if parent is None or parent.agent_id is None:
        return await fail("parent session no longer exists", "parent_not_found", retryable=False)
    try:
        from omnigent.server.routes._sessions.helpers import _require_declared_subagent

        agent = await asyncio.to_thread(deps.agent_store.get, parent.agent_id)
        if agent is not None:
            await asyncio.to_thread(
                _require_declared_subagent,
                agent=agent,
                sub_agent_name=scheduled_helper_type(task.agent_type),
                agent_cache=deps.agent_cache,
            )
    except OmnigentError as exc:
        return await fail(exc.message, "invalid_agent_type", retryable=False)

    try:
        await _wake_parent_runner(deps, parent)
    except Exception as exc:
        _logger.exception("scheduled fire: task %s could not wake the parent runner", task.id)
        return await fail(
            f"helper launch/dispatch failed: {exc}",
            "launch_failed",
            retryable=True,
            reason="your machine didn't start",
        )

    try:
        conv = await fire._create_session(deps, task, scheduled_at=scheduled_at)
    except Exception:
        _logger.exception("scheduled fire: failed to create helper for task %s", task.id)
        return await fail("session creation failed", "session_create_failed", retryable=True)
    conv_id = conv.id
    await fire._attach_cost_budget(deps, task, conv.id)
    # Same grant a ``sys_session_create`` child gets (its creator owns it):
    # the owner-scoped memory routes resolve the session owner from the
    # child's own grants, so a grantless Helper 403s on ``memory/profile``.
    try:
        await fire._grant_owner(deps, task, conv.id)
    except Exception:
        _logger.exception("scheduled fire: owner grant failed for helper %s", conv.id)
    dispatch = fire_dispatch.helper or fire_dispatch.connected
    try:
        await dispatch(conv, task)
    except Exception as exc:
        _logger.exception("scheduled fire: helper dispatch failed for task %s", task.id)
        return await fail(f"helper launch/dispatch failed: {exc}", "launch_failed", retryable=True)
    await fire._record_run(deps, task, conv.id, scheduled_at, status="running", attempt=attempt)
    _logger.info("scheduled fire: task %s started helper %s", task.id, conv.id)
    return conv.id


async def _helper_harness(deps: FireDeps, parent: Conversation, sub_agent_name: str) -> str | None:
    """The harness the bundle gives the Helper Type ``sub_agent_name`` (the bundle's own for a
    Type it does not declare; ``None`` if unknown)."""
    if deps.agent_cache is None or parent.agent_id is None:
        return None
    from omnigent.models.model_catalog import spec_harness
    from omnigent.runtime.workflow import _find_spec_by_name

    try:
        agent = await asyncio.to_thread(deps.agent_store.get, parent.agent_id)
        if agent is None:
            return None
        loaded = await asyncio.to_thread(deps.agent_cache.load, agent.id, agent.bundle_location)
        sub = _find_spec_by_name(loaded.spec, sub_agent_name)
        # A Type the bundle does not declare runs on the bundle's own harness.
        return spec_harness(sub if sub is not None else loaded.spec)
    except Exception:
        _logger.exception("scheduled fire: could not resolve the Helper harness; using defaults")
        return None


async def _conversation_model(deps: FireDeps, parent: Conversation) -> str | None:
    """The model *parent* runs (its pin, else its owner's Foundation), the one answer the turn's
    dispatch uses; ``None`` when unknown."""
    if deps.app_state is None:
        return parent.model_override
    from omnigent.server.routes.sandbox_inference import session_model

    try:
        return await session_model(deps.app_state, parent, deps.conversation_store)
    except Exception:
        _logger.warning("scheduled fire: the model of %s unreadable", parent.id, exc_info=True)
        return parent.model_override


async def _create_helper_session(
    deps: FireDeps, task: ScheduledTask, scheduled_at: int, *, wake_parent: bool = True
) -> Conversation:
    """Create the task's Helper as a sub-agent child of its parent session.

    Mirrors a ``sys_session_create`` child: ``kind="sub_agent"``, the parent's
    agent and runner (co-location), ``sub_agent_name`` = the Type, the parent's
    context-mode labels, and (for a run that wakes its parent) a dispatch id so a
    Result the parent has not drained is recovered. The scheduled-task label lets
    the runner register the Result for the parent wake on the child's first turn. A run whose
    Result code posts is marked unattended: a policy that would ask it denies instead.
    """
    assert task.parent_session_id is not None
    parent = await asyncio.to_thread(
        deps.conversation_store.get_conversation, task.parent_session_id
    )
    if parent is None:
        raise RuntimeError(f"parent session {task.parent_session_id!r} not found")
    labels = dict(inheritable_context_labels(parent.labels))
    if wake_parent and not posts_by_code(task.kind):
        labels[SCHEDULED_HELPER_LABEL_KEY] = task.id
        labels[SUBAGENT_DISPATCH_ID_LABEL_KEY] = f"subagent_{fire._new_id()[:12]}"
    else:
        # No dispatch id: a runner's restart recovery would read the Result as undrained and
        # wake the Conversation with it (ADR 0038, amended 2026-10-25).
        labels[ADHOC_HELPER_LABEL_KEY] = task.id
        if posts_by_code(task.kind):
            labels[UNATTENDED_LABEL_KEY] = "true"
    # ``"<agent_type>:<task name>"``: Activity splits on the first colon, so it reads
    # "<Type>: <task name>" instead of an opaque id (format owned by superchat.activity.titles).
    run_type = scheduled_helper_type(task.agent_type)
    from omnigent.superchat.models.tiers import tier_models

    harness = await _helper_harness(deps, parent, run_type)
    tiers = await tier_models(deps.app_state, task.user_id, harness)
    conversation_model = (
        await _conversation_model(deps, parent)
        if task.model_override == CONVERSATION_MODEL
        else None
    )
    _, run_model = resolve_scheduled_helper(
        task.agent_type, task.model_override, tiers, conversation_model
    )
    # No usable catalog for this harness, or the parent's snapshot does not serve the model:
    # inherit the parent's model like a dispatch does.
    from omnigent.server.routes.sandbox_inference import snapshot_serves_model

    if run_model is not None and not snapshot_serves_model(parent.inference_snapshot, run_model):
        run_model = None
    run_model = run_model or parent.model_override
    conv: Conversation | None = None
    for title in fire_title_candidates(task.agent_type, task.name, task.id, scheduled_at):
        try:
            conv = await asyncio.to_thread(
                deps.conversation_store.create_conversation,
                kind="sub_agent",
                title=title,
                parent_conversation_id=parent.id,
                agent_id=parent.agent_id,
                runner_id=parent.runner_id,
                sub_agent_name=run_type,
                labels=labels,
                model_override=run_model,
                reasoning_effort=task.reasoning_effort,
            )
            break
        except NameAlreadyExistsError:
            continue
    if conv is None:
        raise RuntimeError("helper title collision")
    return conv


def scheduled_brief(task: ScheduledTask, conv: Conversation) -> str:
    """
    The prompt a run starts with; an automation run says it is one, by name.

    A kind with a registered brief (:func:`register_run_brief`) starts with what its hook builds
    instead. Blocking: the hook may read stores.

    The Result goes back to the person's chat unprompted, so it has to say where it comes from:
    otherwise the chat reads it as work nobody asked for.

    :param task: The scheduled task being fired.
    :param conv: The Helper session the run executes in.
    :returns: The task prompt, prefixed for a run that reports back to its parent.
    """
    labels = getattr(conv, "labels", None) or {}
    code_posted = posts_by_code(getattr(task, "kind", None)) and ADHOC_HELPER_LABEL_KEY in labels
    if SCHEDULED_HELPER_LABEL_KEY not in labels and not code_posted:
        return task.prompt
    hook = _RUN_BRIEFS.get(getattr(task, "kind", None) or "")
    if hook is not None:
        try:
            built = hook(task)
        except Exception:
            _logger.exception("scheduled fire: run brief hook failed for task %s", task.id)
            built = None
        if built:
            return built
    return (
        f'This is the person\'s automation "{task.name}", running on its schedule (not a '
        f'message in chat). Begin your final answer with: Automation "{task.name}": '
        f"\n\n{task.prompt}"
    )


def _make_parent_runner_dispatch(
    deps: FireDeps, *, event: SessionEventInput | None = None
) -> LaunchDispatch:
    """Build the Helper dispatch seam: send the prompt (or *event*) to the parent's own runner."""

    async def _dispatch(conv: Conversation, task: ScheduledTask) -> None:
        from omnigent.server.routes.sessions import (
            _dispatch_session_event_to_runner,
            _ensure_runner_relay_ready,
            _ensure_runner_session_initialized,
            _wait_for_runner_client,
        )

        if conv.runner_id is None:
            raise RuntimeError("the parent session has no runner")
        runner_client = await _wait_for_runner_client(
            conv.id,
            deps.runner_router,
            deps.tunnel_registry,
            runner_id=conv.runner_id,
            timeout_s=fire._RUNNER_CONNECT_TIMEOUT_S,
        )
        if runner_client is None:
            raise RuntimeError("the parent's runner is not connected")
        owner = task.user_id or RESERVED_USER_LOCAL
        fresh = await asyncio.to_thread(deps.conversation_store.get_conversation, conv.id)
        conv_for_dispatch = fresh or conv
        await _ensure_runner_session_initialized(
            conv.id, conv_for_dispatch, runner_client, deps.conversation_store
        )
        # Subscribe the server relay BEFORE the prompt goes out: the relay is
        # what sees the turn's idle/failed edge and flips the run row to
        # succeeded/failed. The parent's runner is already connected, so
        # nothing else starts a relay for this fresh child.
        await _ensure_runner_relay_ready(
            conv.id, conv_for_dispatch.runner_id, runner_client, deps.conversation_store
        )
        await _dispatch_session_event_to_runner(
            conv.id,
            conv_for_dispatch,
            event or fire._prompt_event(await asyncio.to_thread(scheduled_brief, task, conv)),
            deps.conversation_store,
            runner_client,
            agent_name=None,
            file_store=deps.file_store,
            artifact_store=deps.artifact_store,
            created_by=owner,
        )

    return _dispatch


async def start_adhoc_helper(
    deps: FireDeps,
    *,
    parent_session_id: str,
    agent_type: str,
    name: str,
    user_id: str | None,
    content: list[dict[str, Any]],
) -> str:
    """Start a Helper of a session for one piece of work now, outside any schedule.

    Unlike a scheduled fire it is not gated by the owner's proactivity or quiet hours (the person
    asked for the work), and its Result does not wake the parent: the work records its output
    itself (e.g. through a tool). *content* is the Helper's first message, as Responses content
    blocks (``input_text``, ``input_image``).

    :param deps: The server dependencies the fire path uses.
    :param parent_session_id: The session the Helper is a child of.
    :param agent_type: The declared Sub-agent Type to run, e.g. ``"teacher"``.
    :param name: Short title for the Helper's Activity entry.
    :param user_id: Owner for the Helper's grant; ``None`` in single-user mode.
    :param content: First message content blocks.
    :returns: The Helper's session id.
    :raises OmnigentError: When the Type is not declared by the parent's agent.
    :raises RuntimeError: When the parent is gone or its runner is not connected.
    """
    parent = await asyncio.to_thread(deps.conversation_store.get_conversation, parent_session_id)
    if parent is None or parent.agent_id is None:
        raise RuntimeError("parent session no longer exists")
    from omnigent.server.routes._sessions.helpers import _require_declared_subagent

    agent = await asyncio.to_thread(deps.agent_store.get, parent.agent_id)
    if agent is not None:
        await asyncio.to_thread(
            _require_declared_subagent,
            agent=agent,
            sub_agent_name=scheduled_helper_type(agent_type),
            agent_cache=deps.agent_cache,
        )
    task = ScheduledTask(
        id=fire._new_id(),
        name=name,
        prompt="",
        rrule="",
        user_id=user_id,
        agent_id=parent.agent_id,
        timezone="UTC",
        created_at=int(time.time()),
        parent_session_id=parent_session_id,
        agent_type=agent_type,
    )
    conv = await _create_helper_session(deps, task, int(time.time()), wake_parent=False)
    try:
        await fire._grant_owner(deps, task, conv.id)
    except Exception:
        _logger.exception("helper: owner grant failed for %s", conv.id)
    event = SessionEventInput(type="message", data={"role": "user", "content": content})
    await fire._make_parent_runner_dispatch(deps, event=event)(conv, task)
    return conv.id


async def wake_conversation(
    deps: FireDeps,
    task: ScheduledTask,
    content: list[dict[str, Any]],
    *,
    context: dict[str, Any] | None = None,
) -> None:
    """Post a runtime notice into the task's parent Conversation, which starts its turn.

    The same path a scheduled Helper's first message takes, aimed at the parent itself: wake its
    host, then send the message to its runner (a turn starts, or the running one reads it). For an
    engine run (:func:`register_engine_run`) with something worth saying now; the assistant writes
    the words, the notice is data.

    :param deps: The server dependencies the fire path uses.
    :param task: The parent-bound task the notice comes from.
    :param content: The notice as Responses content blocks.
    :param context: Stored with the message for the engine's own readers
        (``ON_MESSAGE_CONTEXT``), e.g. ``{"proactive": {"kind": "automation"}}``.
    :raises RuntimeError: When the parent is gone or its host could not be brought online.
    """
    assert task.parent_session_id is not None
    parent = await asyncio.to_thread(
        deps.conversation_store.get_conversation, task.parent_session_id
    )
    if parent is None or parent.agent_id is None:
        raise RuntimeError("parent session no longer exists")
    parent = await _wake_parent_runner(deps, parent)
    event = SessionEventInput(
        type="message",
        data={"role": "user", "is_system_notice": True, "content": content},
        context=context,
    )
    await _make_parent_runner_dispatch(deps, event=event)(parent, task)


# Imported last: ``fire`` imports this module for its names, so it must see them defined first.
from omnigent.server.scheduled import fire  # noqa: E402
