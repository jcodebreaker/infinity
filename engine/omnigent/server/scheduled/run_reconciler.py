"""Scheduled-task run-completion stale backstop (lazy-on-read only).

The fire path (:mod:`omnigent.server.scheduled.fire`) records a
``scheduled_task_runs`` row as ``running`` and returns immediately, WITHOUT
waiting for the agent turn to finish.

**The PRIMARY completion mechanism is event-driven** and lives elsewhere:
:func:`omnigent.server.session_live_state.persist_scheduled_run_completion`,
fired from ``_publish_status`` the instant a fired conversation's turn reaches
a terminal edge, flips the run ``running`` → ``succeeded``/``failed``. It rides
the same long-lived SSE relay that already persists the conversation's
``live_status`` for a browserless scheduled fire, so it needs no live client
and no periodic poll.

This module is the **sole orphan backstop**: a pure age-based force-fail run
on the READ path. If a run is left ``running`` because its terminal event never
fired (host died mid-turn, or a server restart while a fire was in flight), it
stays ``running`` in the DB — harmless until someone looks — and the next read
that surfaces it force-fails it. :func:`force_fail_stale_runs` is called from
both scheduled-task read endpoints (list + detail), so a stale orphan is
reconciled the moment it would otherwise be shown:

- ``GET /v1/scheduled-tasks/{id}/runs`` — force-fails that task's runs still
  ``running`` past :data:`STALE_RUN_MAX_AGE_SECONDS`.
- ``GET /v1/scheduled-tasks`` — force-fails the owner's tasks' stale ``running``
  runs, so a future Tasks-list "last-run status" badge never shows a stale
  orphan as ``running``.

This is a pure age check — NO conversation I/O on the read path. A force-failed run's
work-queue job (ADR 0038) is released with it, so the owner's slot frees at once; the queue's own
reaper does the same for runs nobody reads. The idempotent,
conditional :meth:`update_run` (``WHERE status = running``) means a run already
terminal (via the event hook, a fire-time ``skipped``/``failed``, or a prior
read) is never clobbered. There is deliberately NO startup sweep and NO periodic
poll of any cadence: the event hook handles every normal run, and lazy-on-read
reconciles anything a user actually views.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.entities import ScheduledTaskRun
    from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

# A run still ``running`` longer than this is force-failed with
# ``error_code = "incomplete"`` (host died mid-turn, runner never reported
# completion). Deliberately generous (6h) so a legitimately long agent turn is
# never killed; a stuck-``running`` row is a far milder bug than a
# falsely-``failed`` one.
STALE_RUN_MAX_AGE_SECONDS: int = 6 * 60 * 60

# error_code recorded on the stale-run force-fail path.
STALE_RUN_ERROR_CODE: str = "incomplete"


def _release_queue_slot(conversation_id: str) -> None:
    """Fail the work-queue job bound to a force-failed run (ADR 0038); best-effort."""
    from omnigent.server import work_queue

    try:
        work_queue.default().release_run(conversation_id)
    except Exception:
        _logger.warning(
            "could not release the work-queue job of %s", conversation_id, exc_info=True
        )


def force_fail_stale_runs(
    store: ScheduledTaskStore,
    runs: list[ScheduledTaskRun],
    *,
    now: int | None = None,
    on_force_failed: Callable[[ScheduledTaskRun], None] | None = None,
) -> list[ScheduledTaskRun]:
    """Force-fail ``running`` runs older than the max age; return the list.

    The lazy-on-read orphan backstop, shared by the scheduled-task list and
    detail read endpoints. Pure age check — NO conversation I/O. The age is
    measured from ``fired_at`` (when dispatch actually began), falling back to
    ``scheduled_at`` when a run has no ``fired_at`` (never dispatched). Measuring
    from ``fired_at`` means a run that fired late doesn't get a shortened
    effective window — the 6h clock starts when the turn actually started, not
    when it was scheduled. Only rows past :data:`STALE_RUN_MAX_AGE_SECONDS` are
    touched; the store's conditional :meth:`update_run` (``WHERE status =
    running``) makes it idempotent and safe against a run that just transitioned
    via the event hook. Must be called inside the runs' ``workspace_scope`` (the
    store filters every query on ``current_workspace_id()``); the read endpoints
    already run there.

    The returned list reflects any transition (a force-failed run carries its
    new terminal state) so a caller rendering the runs stays consistent with the
    write; a caller that only needs the side effect can ignore the return.

    :param store: The scheduled-task store to transition runs through.
    :param runs: Candidate runs (typically a task's history, or an owner's
        running runs).
    :param now: Unix epoch seconds to age against; defaults to ``time.time()``.
    :param on_force_failed: Called with each run this call force-failed (e.g. to
        schedule the single retry of a parent-bound task).
    :returns: ``runs`` with any stale ``running`` row replaced by its terminal
        form.
    """
    ts = int(time.time()) if now is None else now
    result: list[ScheduledTaskRun] = []
    for run in runs:
        # Age from when dispatch began (fired_at); fall back to scheduled_at for
        # a run that somehow never recorded a fire time.
        age_from = run.fired_at if run.fired_at is not None else run.scheduled_at
        if run.status == "running" and (ts - age_from) >= STALE_RUN_MAX_AGE_SECONDS:
            updated = store.update_run(
                run.id,
                status="failed",
                finished_at=ts,
                error=(
                    "scheduled run did not reach a terminal state within "
                    f"{STALE_RUN_MAX_AGE_SECONDS}s"
                ),
                error_code=STALE_RUN_ERROR_CODE,
            )
            if updated is not None and run.conversation_id is not None:
                # Its work-queue job held the owner's slot: free it now.
                _release_queue_slot(run.conversation_id)
            if updated is not None:
                from omnigent.server.scheduled.helper_fire import report_run_result

                task = store.get(updated.scheduled_task_id)
                if task is not None:
                    report_run_result(task, updated.conversation_id, "failed")
            if updated is not None and on_force_failed is not None:
                on_force_failed(updated)
            result.append(updated if updated is not None else run)
        else:
            result.append(run)
    return result
