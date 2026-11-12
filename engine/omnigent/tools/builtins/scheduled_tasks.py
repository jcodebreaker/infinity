"""Built-in ``schedule`` tool: create, list, update and delete scheduled tasks.

A scheduled task fires an agent session on a recurring RRULE schedule. The runner
dispatches each action to the Omnigent server's ``/v1/scheduled-tasks`` REST
endpoints (same posture as the policy / session-query tools) — the runner has no
in-process store. A flat schema cannot require a field per action, so the runner
handler checks each action's required fields.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

#: The fields each action needs; the runner handler names the missing one.
SCHEDULE_REQUIRED = {
    "create": ("name", "prompt", "rrule"),
    "list": (),
    "update": ("scheduled_task_id",),
    "delete": ("scheduled_task_id",),
}

_RRULE_DESC = (
    "RFC 5545 recurrence rule, e.g. 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0' (daily at "
    "9am) or 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0' (weekday "
    "mornings). Must fire at least twice and no more often than once per hour. The rule's "
    "clock times (BYHOUR/BYMINUTE) are in the task's timezone. The response lists "
    "`next_fire_times` in that timezone: read them back to the person as the confirmation."
)


_STARTS_ON_DESC = (
    "First day the schedule may fire, as YYYY-MM-DD in the task's timezone: pass the start the "
    "person gave ('starting next week' -> that date). An interval rule such as "
    "'FREQ=WEEKLY;INTERVAL=2;BYDAY=FR' counts its periods from this day. Omit to start now; "
    "changing 'rrule' without it restarts the count from the moment of the edit. Send the rule "
    "alone: no DTSTART line."
)


class ScheduleTool(Tool):
    """Manage scheduled tasks. Runner-dispatched to ``/v1/scheduled-tasks``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"schedule"``."""
        return "schedule"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Scheduled tasks: a saved prompt that runs on a recurring schedule until deleted. "
            "create needs name, prompt and rrule; update and delete need scheduled_task_id "
            "(from list). update changes only the fields passed; state 'paused' stops it "
            "firing, 'active' resumes it."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(SCHEDULE_REQUIRED)},
                        "scheduled_task_id": {"type": "string"},
                        "name": {
                            "type": "string",
                            "description": "Task name, e.g. 'nightly triage'.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "The instruction run on each firing.",
                        },
                        "rrule": {"type": "string", "description": _RRULE_DESC},
                        "starts_on": {"type": "string", "description": _STARTS_ON_DESC},
                        "timezone": {
                            "type": "string",
                            "description": (
                                "IANA timezone the rule is evaluated in, e.g. "
                                "'America/Los_Angeles'. Omit to use the owner's own "
                                "timezone (UTC when none is set)."
                            ),
                        },
                        "state": {"type": "string", "enum": ["active", "paused"]},
                        "parent_session_id": {
                            "type": "string",
                            "description": (
                                "The chat each firing reports to as a Helper: 'self' (the "
                                "default) or a session id you own."
                            ),
                        },
                        "agent_type": {
                            "type": "string",
                            "description": (
                                "Sub-agent Type the bound Helper runs as. Defaults to 'worker'."
                            ),
                        },
                        "kind": {
                            "type": "string",
                            "description": (
                                "Optional product marker, e.g. 'followed_topic' for a "
                                "topic you follow for the person."
                            ),
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        }
