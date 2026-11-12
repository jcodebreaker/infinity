"""The ``checklist`` tool: the model's own list of its steps, on a harness with no native one.

The runner answers it with the item's id; the Round's recorder reads the call the same way it
reads Claude's native task tools, so both write one ``checklist.*`` event shape. Words only: it
is never bound to a Helper and drives nothing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from omnigent.harnesses.executor import CHECKLIST_TOOL
from omnigent.superchat.feature import HandlerCtx
from omnigent.tools.base import Tool

#: The tool's statuses and the native ones the Round view keeps.
STATUSES = {"todo": "pending", "doing": "in_progress", "done": "completed"}


class ChecklistTool(Tool):
    """Add an item to the model's checklist, or change one."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return CHECKLIST_TOOL

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Your task list for work with several steps, shown to the person beside your work. "
            "Without item_id it adds an item and returns its id; with item_id it changes that "
            "item, usually its status as you start and finish it."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        text = {"type": "string"}
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subject": {**text, "description": "The step, e.g. 'Draft the outline'."},
                        "active_form": {
                            **text,
                            "description": "How it reads while it runs: 'Drafting the outline'.",
                        },
                        "status": {"type": "string", "enum": list(STATUSES)},
                        "item_id": {**text, "description": "The item to change."},
                    },
                    "additionalProperties": False,
                },
            },
        }


async def handle_checklist(_ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """:returns: ``{"item_id"}`` of the item added or changed, or ``{"error"}``."""
    item_id = args.get("item_id")
    if args.get("status") not in (None, *STATUSES):
        return json.dumps({"error": f"status must be one of {', '.join(STATUSES)}"})
    if not item_id and not args.get("subject"):
        return json.dumps({"error": "a new item needs a subject"})
    return json.dumps({"item_id": str(item_id or uuid.uuid4().hex[:6])})
