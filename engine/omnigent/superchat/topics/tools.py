"""Built-in tool for topics (ADR 0035): ``topics_update``, offered to Helpers and to the
Conversation, which may write only the topic the person picked up (the server checks).

Schema-only: the runner dispatches it to ``POST /v1/topics/write`` (see ``handlers``). The
quiet brief lists the person's current topics, so there is no read tool.
"""

from __future__ import annotations

from typing import Any

from omnigent.superchat.topics.topic import (
    COVERED_KINDS,
    MAX_COVERED,
    MAX_EVENTS,
    MAX_OPEN,
    MAX_STARTERS,
    MAX_TOPICS_PER_CALL,
    MAX_WATCH_TERMS,
    OPEN_KINDS,
    SOURCE_KEYS,
    STATUSES,
)
from omnigent.tools.base import Tool

TOPICS_UPDATE_TOOL_NAME = "topics_update"
TOPIC_TOOL_NAMES = (TOPICS_UPDATE_TOOL_NAME,)

_STR: dict[str, Any] = {"type": "string"}
_STRS: dict[str, Any] = {"type": "array", "items": _STR}

_TOPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": (
                "Stable slug (lowercase, hyphens). Reuse the key listed in the brief when the "
                "topic comes back; the working list's `t-` tag when it has one."
            ),
        },
        "title": {"type": "string", "description": "2 to 5 words in the person's terms."},
        "left_off": {"type": "string", "description": "One sentence: where it stands now."},
        "tag": {
            "type": "string",
            "description": (
                "A short name for the topic on Home, 1 or 2 words (a client, a place, a project "
                "codename): 'Morocco', 'Polaris'. Keep it once set."
            ),
        },
        "status_line": {
            "type": "string",
            "description": (
                "What the person sees under the topic on Home: where the work stands now, one "
                "short sentence of at most 80 characters in their terms, what just happened and "
                "what is next: 'Phone check done; 3 layout fixes next', 'Reply to Dana drafted, "
                "yours to send', 'Done. Nothing open'. Never a copy of an open promise (Home "
                "shows those on their own), never a date in digits, never 'new' or 'overdue'."
            ),
        },
        "covered": {
            "type": "array",
            "maxItems": MAX_COVERED,
            "description": "What was covered, newest first.",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "kind": {"type": "string", "enum": list(COVERED_KINDS)},
                    "text": _STR,
                    "file": {"type": "string", "description": "File name, when one was made."},
                },
                "required": ["date", "kind", "text"],
            },
        },
        "open": {
            "type": "array",
            "maxItems": MAX_OPEN,
            "description": (
                "Open loops and promises, passed whole. Drop a line when it is done (it moves to "
                "`covered`)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": (
                            "The line's id as listed; pass it back to keep the line, even when "
                            "you reword it. Leave it out for a new line: code assigns one."
                        ),
                    },
                    "kind": {"type": "string", "enum": list(OPEN_KINDS)},
                    "text": _STR,
                    "due": {
                        "type": "string",
                        "description": (
                            "YYYY-MM-DD, only when the conversation gives a real date for it; "
                            "never guessed. A line that keeps its id keeps its stored date."
                        ),
                    },
                    "starter": {
                        "type": "string",
                        "description": (
                            "The request that takes this line on, as the person would type it: "
                            "'Draft my message to Dana with the link'. Kept when left out."
                        ),
                    },
                    "verb": {
                        "type": "string",
                        "description": (
                            "The starter's button, 1 or 2 words: 'Draft it', 'Fix', 'Decide'. "
                            "Kept when left out."
                        ),
                    },
                },
                "required": ["kind", "text"],
            },
        },
        "starters": {
            "type": "array",
            "maxItems": MAX_STARTERS,
            "items": _STR,
            "description": (
                "Next requests in the person's voice, one line each, only for a next step no "
                "open line covers (each open line carries its own `starter`); often none."
            ),
        },
        "events": {
            "type": "array",
            "maxItems": MAX_EVENTS,
            "description": "Dated one-line moments; merged with the stored ones.",
            "items": {
                "type": "object",
                "properties": {
                    "at": {
                        "type": "string",
                        "description": "When it happened: YYYY-MM-DDTHH:MM (local time).",
                    },
                    "line": _STR,
                },
                "required": ["at", "line"],
            },
        },
        "sources": {
            "type": "object",
            "properties": {key: {"type": "integer"} for key in SOURCE_KEYS},
            "description": "How many chats, files and emails it drew on.",
        },
        "watch": {
            "type": "object",
            "description": "What identifies the topic in new mail, events, files or news.",
            "properties": {
                "people": {**_STRS, "description": "Names or email addresses involved."},
                "terms": {
                    **_STRS,
                    "maxItems": MAX_WATCH_TERMS,
                    "description": "Project, client or deal names that identify it.",
                },
                "refs": {**_STRS, "description": "Thread, event or file ids it touched."},
                "web": {
                    "type": "boolean",
                    "description": "True only when it needs outside news (markets, a public "
                    "company, a regulation).",
                },
            },
        },
        "status": {"type": "string", "enum": list(STATUSES)},
    },
    "required": ["key"],
}


class TopicsUpdateTool(Tool):
    """Upsert the person's topics by key; dispatched to ``POST /v1/topics/write``."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return TOPICS_UPDATE_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Create or update the person's topics, by key. Pass only the fields that changed; a "
            "field passed replaces the stored one, except `events`, which are added to the "
            "stored ones. A new topic needs `title`. Set `status: closed` when it is done."
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
                        "topics": {
                            "type": "array",
                            "maxItems": MAX_TOPICS_PER_CALL,
                            "items": _TOPIC_SCHEMA,
                        }
                    },
                    "required": ["topics"],
                    "additionalProperties": False,
                },
            },
        }
