"""Built-in tools for the daily note (talked about, decisions, promised, open loops).

Schema-only classes: the runner dispatches each to the server's daily-note REST endpoints
(see ``_execute_daily_note_tool``). Offered to Helpers only.

* ``daily_note_get`` — read today's note.
* ``daily_note_update`` — merge sections into today's note. Text the person edited is kept.
"""

from __future__ import annotations

from typing import Any

from omnigent.entities.daily_note import SECTION_KEYS
from omnigent.tools.base import Tool

DAILY_NOTE_TOOL_NAMES = ("daily_note_get", "daily_note_update")


class _DailyNoteTool(Tool):
    _NAME = ""
    _DESC = ""
    _PROPERTIES: dict[str, Any] = {}

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return cls._NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return cls._DESC

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": self._PROPERTIES,
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }


class DailyNoteGetTool(_DailyNoteTool):
    """Read today's note; dispatched to ``GET /v1/me/daily-notes/today``."""

    _NAME = "daily_note_get"
    _DESC = "Read the person's note for today (their local day), with each section's text."


class DailyNoteUpdateTool(_DailyNoteTool):
    """Merge sections into today's note; dispatched to ``POST /v1/daily-notes/write``."""

    _NAME = "daily_note_update"
    _DESC = (
        "Update the person's note for today. Pass only the sections you want to change, each as "
        "the section's full new text (short bullet lines). A section the person edited is never "
        "rewritten: your new lines are only added to it."
    )
    _PROPERTIES = {
        key: {"type": "string", "description": f"Full text of the `{key}` section."}
        for key in SECTION_KEYS
    } | {
        "date": {
            "type": "string",
            "description": "YYYY-MM-DD of today or yesterday; omit for today.",
        }
    }
