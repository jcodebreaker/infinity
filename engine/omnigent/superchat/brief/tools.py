"""The brief run's one tool (schema only; the runner-side handler saves the page)."""

from __future__ import annotations

from omnigent.superchat.artifacts.tools import ArtifactTool
from omnigent.superchat.brief.page import MAX_DAY, MAX_HOME, MAX_NEEDS, MAX_SINCE, MAX_WORTH

SAVE_TOOL_NAME = "brief_save"
BRIEF_TOOL_NAMES = (SAVE_TOOL_NAME,)

_LINE = {"type": "string"}


class BriefSaveTool(ArtifactTool):
    _NAME = SAVE_TOOL_NAME
    _DESC = (
        "Save the morning brief as a new version of the person's brief page. You give the "
        "content; the page's layout is fixed. Leave out a section with nothing in it. Returns the "
        "saved page with its `title` and `line` (the first thing that needs the person, or "
        "'Nothing needs you today')."
    )
    _PROPERTIES = {
        "date": {"type": "string", "description": "The brief's day, YYYY-MM-DD."},
        "summary": {"type": "string", "description": "One plain sentence for the top."},
        "needs_you": {
            "type": "array",
            "maxItems": MAX_NEEDS,
            "description": "What needs the person today, most pressing first.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {**_LINE, "description": "What needs them, in a few words."},
                    "why": {**_LINE, "description": "Why today, one sentence."},
                    "tag": {**_LINE, "description": "A short when, e.g. 'Before 10:00'."},
                    "source": {**_LINE, "description": "Where it came from, e.g. 'Note, Thu'."},
                    "next_step": {**_LINE, "description": "One concrete next step."},
                    "draft": {
                        "type": "object",
                        "description": "A ready draft the person sends themselves; never sent.",
                        "properties": {"to": _LINE, "text": _LINE},
                        "required": ["text"],
                    },
                },
                "required": ["title", "why"],
            },
        },
        "your_day": {
            "type": "array",
            "maxItems": MAX_DAY,
            "description": "Today's meetings in time order.",
            "items": {
                "type": "object",
                "properties": {
                    "time": {**_LINE, "description": "Local start, e.g. '10:00'."},
                    "title": _LINE,
                    "prep": {**_LINE, "description": "One line of prep."},
                },
                "required": ["time", "title"],
            },
        },
        "since_yesterday": {
            "type": "array",
            "maxItems": MAX_SINCE,
            "description": "What moved on open topics, one line each.",
            "items": {
                "type": "object",
                "properties": {"topic": _LINE, "text": _LINE},
                "required": ["text"],
            },
        },
        "home_lines": {
            "type": "array",
            "maxItems": MAX_HOME,
            "description": (
                "The person's Home shows these under their topics: one line per topic that moved "
                "or needs them, most pressing first."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "topic_key": {**_LINE, "description": "The topic's key as listed."},
                    "topic": {**_LINE, "description": "The topic's title as listed."},
                    "text": {
                        **_LINE,
                        "description": (
                            "First half, plain: what happened or what was done, one short "
                            "clause ('Review of all 14 slides finished.')."
                        ),
                    },
                    "bold": {
                        **_LINE,
                        "description": (
                            "Second half, shown bold: what is ready for them or what needs "
                            "them ('3 slides need your eye.', 'It still needs you.')."
                        ),
                    },
                },
                "required": ["topic_key", "text", "bold"],
            },
        },
        "worth_knowing": {
            "type": "array",
            "maxItems": MAX_WORTH,
            "description": "Short things worth knowing that need no action.",
            "items": _LINE,
        },
    }
    _REQUIRED = ("date",)
