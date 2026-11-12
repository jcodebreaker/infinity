"""The brief's scheduled task: its kind, its derived id and what it is made with (ADR 0037).

No imports beyond the standard library: the runner-side handler reads the id too.
"""

from __future__ import annotations

import hashlib
import uuid

#: The scheduled task's kind; the outbox (0036) labels its run's message with this kind and,
#: by kind, urgency ``brief``.
BRIEF_KIND = "daily_brief"
BRIEF_TASK_NAME = "Morning brief"
#: Weekdays at 7:30, in the task's time zone: the default before it is spread per person.
BRIEF_RRULE = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=30"
#: How far a person's brief is moved from 7:30 so everyone's does not fire at once (ADR 0038).
BRIEF_SPREAD_MINUTES = 10
#: The Helper Type a run uses: the general one, as a live page's update does.
BRIEF_AGENT_TYPE = "worker"
#: The model of a run: the one the person's Conversation runs, else their Builder (ADR 0040;
#: ``omnigent.superchat.subagents.CONVERSATION_MODEL``, spelled out: standard library only).
BRIEF_MODEL = "conversation"
#: What a brief task made before ADR 0040 stores; moved to :data:`BRIEF_MODEL`.
BRIEF_MODEL_BEFORE = "builder"
#: What the task holds as its prompt; each run starts with :func:`build_brief` instead.
BRIEF_PROMPT = (
    "Write the person's morning brief with brief_save: what needs them today, their day, what "
    "moved since yesterday and what is worth knowing, from their notes, topics and memory."
)


def brief_task_id(conversation_id: str) -> str:
    """The task id for a Conversation's brief: derived, so two first uses make one row."""
    return uuid.uuid5(uuid.NAMESPACE_OID, f"omnigent-daily-brief\0{conversation_id}").hex


def brief_rrule(owner: str) -> str:
    """The person's own default brief time: 7:30 moved by up to :data:`BRIEF_SPREAD_MINUTES`
    either way, derived from *owner* so it is stable (ADR 0038, "The brief keeps its
    Automation")."""
    span = 2 * BRIEF_SPREAD_MINUTES + 1
    shift = int(hashlib.sha256(owner.encode()).hexdigest(), 16) % span - BRIEF_SPREAD_MINUTES
    return f"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE={30 + shift}"
