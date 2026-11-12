"""Built-in tools for managing scheduled tasks (recurring agent runs).

A scheduled task fires an agent session on a recurring RRULE schedule. These
tools let an agent create, list, update, and delete its own scheduled tasks. The
runner dispatches each to the Omnigent server's ``/v1/scheduled-tasks`` REST
endpoints (same posture as the policy / session-query tools) — the runner has no
in-process store.

* ``sys_scheduled_task_create`` — create a recurring task.
* ``sys_scheduled_task_list`` — list the caller's tasks.
* ``sys_scheduled_task_update`` — update a task's mutable fields.
* ``sys_scheduled_task_delete`` — delete a task.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

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


class SysScheduledTaskCreateTool(Tool):
    """Create a scheduled task. Runner-dispatched to ``POST /v1/scheduled-tasks``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_scheduled_task_create"``."""
        return "sys_scheduled_task_create"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Create a scheduled task: a saved prompt that runs an agent session "
            "on a recurring schedule (RRULE). Provide the agent to run, the "
            "prompt to send it, and the recurrence rule. The workspace is always "
            "optional and defaults to the launch host's home directory (fine for "
            "MCP-only / chat tasks that touch no code directory). Optionally PIN "
            "a connected host and/or a workspace on it; with no pinned host it "
            "runs on your live host at fire time (the owner must have an online "
            "host then, else the run is recorded as failed). The task fires "
            "automatically on its schedule until deleted."
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
                        "name": {
                            "type": "string",
                            "description": "Human-readable task name, e.g. 'nightly triage'.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "The instruction dispatched to the agent on each firing."
                            ),
                        },
                        "rrule": {"type": "string", "description": _RRULE_DESC},
                        "starts_on": {"type": "string", "description": _STARTS_ON_DESC},
                        "agent_id": {
                            "type": "string",
                            "description": (
                                "The agent to run, e.g. 'ag_abc123' — from "
                                "sys_agent_list or sys_agent_get. Omit to bind the "
                                "task to this chat."
                            ),
                        },
                        "parent_session_id": {
                            "type": "string",
                            "description": (
                                "Optional. Bind each firing to a chat as a Helper "
                                "child so its Result wakes that chat. Use 'self' for "
                                "this chat (or a session id you own). Defaults to this "
                                "chat when agent_id is omitted."
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
                        "timezone": {
                            "type": "string",
                            "description": (
                                "IANA timezone the rule is evaluated in, e.g. "
                                "'America/Los_Angeles'. Omit to use the owner's own "
                                "timezone (UTC when none is set)."
                            ),
                        },
                        "model_override": {
                            "type": "string",
                            "description": (
                                "Optional per-run model override. Omit for the agent default."
                            ),
                        },
                        "reasoning_effort": {
                            "type": "string",
                            "description": "Optional per-run reasoning-effort hint, e.g. 'high'.",
                        },
                        "permission_mode": {
                            "type": "string",
                            "description": (
                                "Optional permission mode for native coding agents that "
                                "support one (Claude Code): 'default', 'auto', "
                                "'acceptEdits', 'plan', 'dontAsk', or 'bypassPermissions'. "
                                "Runs are unattended, so a prompting mode ('default'/'plan') "
                                "stalls waiting for approval — prefer an auto-running mode. "
                                "Omit for the agent default."
                            ),
                        },
                        "max_cost_usd": {
                            "type": "number",
                            "description": (
                                "Optional per-firing cost budget in USD. When set, each "
                                "fired session is capped at this spend — all models are "
                                "blocked once the limit is reached. Omit for no cap."
                            ),
                        },
                        "workspace": {
                            "type": "string",
                            "description": (
                                "Optional existing absolute path where the run's runner "
                                "starts. Omit to default to the launch host's home "
                                "directory (whether the host is pinned or resolved)."
                            ),
                        },
                        "host_id": {
                            "type": "string",
                            "description": (
                                "Optional PIN of a connected host to run on, from the "
                                "current workspace's host list. Omit to run on the owner's "
                                "online host at fire time; a failed run is recorded if none "
                                "is online. Existing managed sandbox hosts cannot be pinned."
                            ),
                        },
                        "execution_target": {
                            "type": "string",
                            "enum": ["connected_host", "managed_sandbox"],
                            "description": (
                                "Where each firing runs. 'connected_host' (default) uses a "
                                "pinned/resolved connected host. 'managed_sandbox' provisions "
                                "a fresh server-managed sandbox per firing, using the server's "
                                "sandbox lifecycle settings. Do not set host_id or workspace "
                                "with it. Requires managed sandboxes configured on the server."
                            ),
                        },
                    },
                    "required": ["name", "prompt", "rrule"],
                    "additionalProperties": False,
                },
            },
        }


class SysScheduledTaskListTool(Tool):
    """List the caller's scheduled tasks. Dispatched to ``GET /v1/scheduled-tasks``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_scheduled_task_list"``."""
        return "sys_scheduled_task_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "List your scheduled tasks (recurring agent runs) with their schedules and state."

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }


class SysScheduledTaskUpdateTool(Tool):
    """Update a scheduled task. Dispatched to ``PATCH /v1/scheduled-tasks/{id}``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_scheduled_task_update"``."""
        return "sys_scheduled_task_update"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Update a scheduled task's mutable fields. Only the fields you pass "
            "change; omit the rest. Pass agent_id to switch which agent/harness "
            "it runs. Pass state='paused' to stop it firing without deleting it, "
            "or state='active' to resume."
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
                        "scheduled_task_id": {
                            "type": "string",
                            "description": "The task to update (from sys_scheduled_task_list).",
                        },
                        "name": {"type": "string", "description": "New task name."},
                        "prompt": {"type": "string", "description": "New prompt."},
                        "rrule": {"type": "string", "description": _RRULE_DESC},
                        "starts_on": {"type": "string", "description": _STARTS_ON_DESC},
                        "agent_id": {
                            "type": "string",
                            "description": (
                                "Rebind the task to a different agent, switching the "
                                "harness its future firings run (e.g. Cursor to Pi) — "
                                "from sys_agent_list. Past runs keep the agent they "
                                "ran. Model, effort, and permission mode do not carry "
                                "across a switch: resend any you want set."
                            ),
                        },
                        "timezone": {"type": "string", "description": "New IANA timezone."},
                        "model_override": {"type": "string", "description": "New model override."},
                        "reasoning_effort": {
                            "type": "string",
                            "description": "New reasoning-effort hint.",
                        },
                        "permission_mode": {
                            "type": "string",
                            "description": (
                                "New permission mode for native coding agents (Claude "
                                "Code): 'default', 'auto', 'acceptEdits', 'plan', "
                                "'dontAsk', or 'bypassPermissions'."
                            ),
                        },
                        "max_cost_usd": {
                            "type": "number",
                            "description": (
                                "New per-firing cost budget in USD. Null clears the cap."
                            ),
                        },
                        "workspace": {
                            "type": "string",
                            "description": "New existing absolute runner start path.",
                        },
                        "host_id": {
                            "type": "string",
                            "description": "New connected host to run on.",
                        },
                        "execution_target": {
                            "type": "string",
                            "enum": ["connected_host", "managed_sandbox"],
                            "description": (
                                "Switch where firings run. 'managed_sandbox' provisions a "
                                "fresh server-managed sandbox per firing (clears any pinned "
                                "host; do not also set host_id or workspace)."
                            ),
                        },
                        "state": {
                            "type": "string",
                            "description": "'active' or 'paused'.",
                        },
                        "parent_session_id": {
                            "type": "string",
                            "description": (
                                "Rebind to a chat ('self' for this chat); set together "
                                "with agent_type. Null for both unbinds the task."
                            ),
                        },
                        "agent_type": {
                            "type": "string",
                            "description": "Sub-agent Type for the bound Helper.",
                        },
                    },
                    "required": ["scheduled_task_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysScheduledTaskDeleteTool(Tool):
    """Delete a scheduled task. Dispatched to ``DELETE /v1/scheduled-tasks/{id}``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_scheduled_task_delete"``."""
        return "sys_scheduled_task_delete"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "Delete a scheduled task so it no longer fires."

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
                        "scheduled_task_id": {
                            "type": "string",
                            "description": "The task to delete (from sys_scheduled_task_list).",
                        },
                    },
                    "required": ["scheduled_task_id"],
                    "additionalProperties": False,
                },
            },
        }
