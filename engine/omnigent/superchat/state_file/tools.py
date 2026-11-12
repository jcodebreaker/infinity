"""Built-in ``state_close`` tool: close one entry of the checkpoint's state file.

Schema-only: the runner dispatches it to ``handlers.handle_state_close``.
"""

from __future__ import annotations

from typing import Any

from omnigent.context.state_file import CLOSE_REASON_MAX_CHARS
from omnigent.tools.base import Tool

STATE_CLOSE_TOOL_NAME = "state_close"


class StateCloseTool(Tool):
    """Close a state-file entry the person said is wrong or should go."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return STATE_CLOSE_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Close one entry of your state file when the person says it is wrong, done or "
            "should be forgotten. Pass its tag, like t-launch-budget, and a short reason. "
            "The entry is closed at the next checkpoint."
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
                        "tag": {
                            "type": "string",
                            "description": "The entry's tag id, with or without brackets.",
                        },
                        "reason": {
                            "type": "string",
                            "maxLength": CLOSE_REASON_MAX_CHARS,
                            "description": "Why, in a few words.",
                        },
                    },
                    "required": ["tag", "reason"],
                    "additionalProperties": False,
                },
            },
        }
