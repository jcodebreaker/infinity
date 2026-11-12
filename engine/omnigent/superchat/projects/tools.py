"""``open_project`` tool definition (schema only; the runner handles the call)."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

OPEN_PROJECT_TOOL_NAME = "open_project"


class OpenProjectTool(Tool):
    """Work inside one Project's folder; dispatched by the runner."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"open_project"``."""
        return OPEN_PROJECT_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Open a Project: from your next tool call on, your shell and file tools start in its "
            "folder (and so do Helpers you start). `slug` is a folder name from the Projects list "
            "in this turn. Pass null to go back to the workspace root."
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
                        "slug": {
                            "type": ["string", "null"],
                            "description": "The Project's folder name, or null for the root.",
                        }
                    },
                    "required": ["slug"],
                    "additionalProperties": False,
                },
            },
        }
