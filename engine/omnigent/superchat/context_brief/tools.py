"""Built-in ``context_brief`` tool: the brief for the current turn, ready at once.

Schema-only: the runner dispatches it to ``runtime.handle_context_brief``.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

CONTEXT_BRIEF_TOOL_NAME = "context_brief"


class ContextBriefTool(Tool):
    """What already exists on the subject of the person's message."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return CONTEXT_BRIEF_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "What already exists on the subject of the person's message: what you made before, "
            "work still running, earlier turns and notes, matching files and standing "
            "preferences. Worth a call when the request resembles earlier work, names a file "
            "or project, or depends on a preference."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
