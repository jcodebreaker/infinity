"""Helper tool definitions (schemas only; the runner handles the calls).

``start_helper`` and ``message_helper`` for the chat that starts Helpers; ``report_progress``
and ``raise_flag`` for a Helper telling that chat how its work goes.
"""

from __future__ import annotations

from typing import Any

from omnigent.superchat.helpers.notices import RAISE_FLAG_TOOL_NAME, REPORT_PROGRESS_TOOL_NAME
from omnigent.superchat.models.tiers import CATEGORIES
from omnigent.superchat.subagents import HELPER_DEFAULT_MODEL, HELPER_EFFORT_CHOICES
from omnigent.tools.base import Tool

START_HELPER_TOOL_NAME = "start_helper"
MESSAGE_HELPER_TOOL_NAME = "message_helper"


class StartHelperTool(Tool):
    """Start one Helper on a self-contained task; dispatched by the runner."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"start_helper"``."""
        return START_HELPER_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Start a Helper on one self-contained piece of work; returns its id at once. Its "
            "result arrives later as a message, so once the brief is sent, say in one sentence "
            "that the work started and end your turn. `task` is the whole brief (it does not see "
            "this conversation); call it once per Helper, several in one step to run them in "
            "parallel, never twice for the same task. `project` or `path` sets the folder the "
            "Helper works in; a job for a Project's folder must pass it, since a path inside the "
            "brief does not move the Helper there."
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
                        "task": {
                            "type": "string",
                            "description": "The complete brief for the Helper.",
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "A short title the person will read for this work, 3 to 6 "
                                "words, in their terms (e.g. 'Counting words in your PDFs'). "
                                "Not the brief."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "enum": list(CATEGORIES),
                            "default": HELPER_DEFAULT_MODEL,
                            "description": (
                                "builder (the default): writing decks, pages, documents, "
                                "sheets. light: fetch-and-compare, lookups, extraction. "
                                "foundation: only for judgement."
                            ),
                        },
                        "reasoning": {
                            "type": "string",
                            "enum": list(HELPER_EFFORT_CHOICES),
                            "description": "How hard it should think. Omit for the default.",
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ids of uploaded files to hand to the Helper.",
                        },
                        "project": {
                            "type": "string",
                            "description": (
                                "Slug of the Project the work belongs to; the Helper works in "
                                "its folder. Omit to keep your current folder."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Workspace-relative folder the Helper works in, instead of "
                                "`project`; made when it does not exist yet."
                            ),
                        },
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
            },
        }


class MessageHelperTool(Tool):
    """Pass a message on to a Helper that is already working; dispatched by the runner."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"message_helper"``."""
        return MESSAGE_HELPER_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Pass a message to a Helper that is already working, such as a correction or an "
            "added detail from the person about that task. The Helper reads it at its next step "
            "and keeps going on the same task; its result still arrives here when it finishes. "
            "Use it instead of starting another Helper. `helper_id` is the id `start_helper` "
            "returned; leave it out when exactly one Helper is running."
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
                        "message": {
                            "type": "string",
                            "description": "What the Helper should know, in the person's words.",
                        },
                        "helper_id": {
                            "type": "string",
                            "description": "The Helper to tell, from `start_helper`.",
                        },
                    },
                    "required": ["message"],
                    "additionalProperties": False,
                },
            },
        }


class _NoticeTool(Tool):
    """One line from a Helper to the chat that started it (``superchat/helpers/notices.py``)."""

    _NAME = ""
    _DESCRIPTION = ""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return cls._NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return cls._DESCRIPTION

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
                        "text": {
                            "type": "string",
                            "description": "One line, in the person's terms.",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }


class ReportProgressTool(_NoticeTool):
    """A Helper says which step it just finished."""

    _NAME = REPORT_PROGRESS_TOOL_NAME
    _DESCRIPTION = (
        "Tell the chat that started you which step you just finished, in one line (e.g. 'Read "
        "the Q3 ledger; the totals match the board pack'). Call it after each real step, not "
        "for every tool call. You keep working; it does not end your task."
    )


class RaiseFlagTool(_NoticeTool):
    """A Helper says something the person should know now."""

    _NAME = RAISE_FLAG_TOOL_NAME
    _DESCRIPTION = (
        "Tell the chat that started you, in one line, something the person should know now: a "
        "choice between sources, a departure from the brief, or a surprise in what you found. "
        "You keep working; it does not end your task."
    )
