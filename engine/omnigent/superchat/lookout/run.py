"""How a Lookout wake starts its one run: the person's hidden ``lookout_wake`` task (ADR 0038).

Each person has one hidden scheduled task of kind :data:`LOOKOUT_KIND`, bound to their
Conversation and kept paused: it never fires on a schedule. A wake queues a ``lookout_wake`` job in
the work queue (:func:`launch_wake`, keyed by the wake id), whose handler runs the task through the
parent-bound fire path, so the run takes the queue's limits (across everyone and per owner), the
fire path's gates, the Helper on the person's machine and the run record. The task carries no
per-run cost cap (ADR 0040): the run keeps the step and time limits of any run and the unattended
policy (read and draft only); a task made with the old cap is re-ensured without it. Its Result is
posted by code (``register_run_result``), never as a wake of the
Conversation. The kind never shows among Automations (the web lists only ``automation``).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from omnigent.server import work_queue
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.superchat.subagents import CONVERSATION_MODEL

_logger = logging.getLogger(__name__)

LOOKOUT_KIND = "lookout_wake"
TASK_NAME = "Lookout"
#: The general Helper Type, as the brief and a live page's update use.
AGENT_TYPE = "worker"
#: One run per wake, on the model the person's Conversation runs (ADR 0040), else their Builder.
MODEL = CONVERSATION_MODEL
#: Never armed (the task stays paused); a valid rule so the row reads like any other task.
_NEVER_RRULE = "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0"
_PROMPT = "Check what came in for the person and say what is worth saying (ADR 0038)."


def wake_task_id(conversation_id: str) -> str:
    """The id of a Conversation's wake task: derived, so two first wakes make one row."""
    return uuid.uuid5(uuid.NAMESPACE_OID, f"omnigent-lookout\0{conversation_id}").hex


def ensure_task(tasks: Any, conversation: Any, user_id: str | None, timezone: str) -> Any:
    """The Conversation's paused ``lookout_wake`` task, made when missing (blocking)."""
    derived = wake_task_id(conversation.id)
    task = tasks.get(derived)
    if task is not None and task.kind == LOOKOUT_KIND and task.state != "deleted":
        if (
            task.timezone != timezone
            or task.agent_id != conversation.agent_id
            or getattr(task, "max_cost_usd", None) is not None
            or task.model_override != MODEL
        ):
            # ``max_cost_usd=None`` clears the cap a task made before ADR 0040 carries, and the
            # Builder model it ran on becomes the Conversation's.
            task = (
                tasks.update(
                    task.id,
                    timezone=timezone,
                    agent_id=conversation.agent_id,
                    max_cost_usd=None,
                    model_override=MODEL,
                )
                or task
            )
        return task
    try:
        return tasks.create(
            scheduled_task_id=derived,
            name=TASK_NAME,
            prompt=_PROMPT,
            rrule=_NEVER_RRULE,
            user_id=user_id,
            agent_id=conversation.agent_id,
            timezone=timezone,
            parent_session_id=conversation.id,
            agent_type=AGENT_TYPE,
            kind=LOOKOUT_KIND,
            model_override=MODEL,
            state="paused",
        )
    except Exception:
        # A concurrent first wake made it: the derived id makes the second insert fail.
        found = tasks.get(derived)
        if found is None:
            raise
        return found


def launch_wake(task: Any, wake_id: str) -> bool:
    """Queue the wake's run (blocking): a ``lookout_wake`` job keyed by *wake_id*, so a repeat
    is folded into the open job; it waits for the owner's room and the limit across everyone.

    :returns: Whether a job is queued (its run reports back through the run-result hook).
    """
    from omnigent.server.scheduled.helper_fire import fire_job_payload

    payload = {
        **fire_job_payload(task, int(time.time()), require_active=False),
        "wake_id": wake_id,
    }
    work_queue.submit(
        task.user_id or RESERVED_USER_LOCAL,
        LOOKOUT_KIND,
        wake_id,
        payload,
        conversation_id=task.parent_session_id,
    )
    return True
