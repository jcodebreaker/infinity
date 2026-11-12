"""Built-in tool for decks: ``deck_export``.

Schema-only class: the runner dispatches it to :mod:`omnigent.superchat.decks.handlers`, which
runs in the person's host.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

DECK_TOOL_NAMES = ("deck_export", "deck_new", "deck_check", "deck_themes", "deck_theme_set")
DECK_AUTHORING_TOOL_NAMES = ("deck_new", "deck_check", "deck_themes", "deck_theme_set")
DECK_EXPORT_FORMATS = ("pptx", "pdf")
#: Where a new deck's template came from (``deck_new``'s ``look_from``), recorded in its result as
#: given (ADR 0026); never a reason to change the template. The one list: ``decks.look`` reads it.
LOOK_SOURCES = ("named", "picked", "you_choose", "background")


class DeckExportTool(Tool):
    """Export a saved deck to PowerPoint or PDF; dispatched to the runner handler."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return "deck_export"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Export a saved `.deck.html` deck to an editable PowerPoint (.pptx) or a vector PDF "
            "and save it as a new file in this chat, which shows its own file card. A deck is an "
            "HTML file; use this only when the person asks for PowerPoint or PDF."
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
                        "artifact_id": {
                            "type": "string",
                            "description": "The deck's artifact id (from artifact_save/list).",
                        },
                        "format": {
                            "type": "string",
                            "enum": list(DECK_EXPORT_FORMATS),
                            "description": "pptx for editable PowerPoint, pdf for a vector PDF.",
                        },
                    },
                    "required": ["artifact_id", "format"],
                    "additionalProperties": False,
                },
            },
        }


class DeckNewTool(Tool):
    """Create a deck file from a template and the model's slides; dispatched to the runner."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return "deck_new"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        from omnigent.superchat.decks import kit

        names = ", ".join(kit.templates())
        return (
            "Create a deck: an HTML `.deck.html` file of 1920x1080 slides, never a PowerPoint "
            "(that is a separate export, deck_export). The layouts are the decks skill. With "
            "all the slides it adds the framework, theme and fonts, writes the file and returns "
            "the layout problems to fix before artifact_save. Without slides it writes nothing: "
            "with `layout` it returns one example slide of that layout or one chart recipe. "
            "Slides are refused, nothing written, when anything in them loads from the web (an "
            "http(s) or // URL in src, url() or an href other than a link's <a href>, a web "
            "font, @import) or they carry a "
            "<script> other than a chart's <script data-superchat-chart>: draw a logo or icon "
            "with HTML, CSS or inline SVG, or leave it out, and use only the theme's fonts. "
            f"Themes (deck_themes says what each is for): {names}."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        from omnigent.superchat.decks import kit

        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Where to write it, ending in .deck.html, "
                                "e.g. your_files/q3-review.deck.html."
                            ),
                        },
                        "template": {
                            "type": "string",
                            "enum": list(kit.templates()),
                            "description": (
                                "The theme to build in, when they named one or a mood. Omit it "
                                "to have the engine choose one for the topic and their taste."
                            ),
                        },
                        "look_from": {
                            "type": "string",
                            "enum": list(LOOK_SOURCES),
                            "description": (
                                "Where the template came from: named (their message names the "
                                "theme or a mood), picked (their answer to a look card), "
                                "you_choose (they said you choose), background (scheduled "
                                "work). Recorded only. The file card's Theme control changes "
                                "the look later."
                            ),
                        },
                        "title": {"type": "string", "description": "The deck's title."},
                        "layout": {
                            "type": "string",
                            "enum": kit.layout_names(),
                            "description": (
                                "Without slides: one example of this layout, or this chart "
                                "recipe, to copy. Nothing is written."
                            ),
                        },
                        "slides": {
                            "type": "string",
                            "description": (
                                "Omit only with layout. Every slide in one string: a "
                                '<section class="slide <surface> <layout>" '
                                'data-screen-label="NN Name" data-superchat-id="..."> per '
                                "slide, using the deck layouts, every text block with its own "
                                "lower-case-hyphenated data-superchat-id; the first slide has "
                                'class "slide active". Nothing from the web, no other script.'
                            ),
                        },
                    },
                    "required": ["path", "title"],
                    "additionalProperties": False,
                },
            },
        }


class DeckCheckTool(Tool):
    """Check a deck file's layout in the host; dispatched to the runner."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return "deck_check"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Check a deck file before you save it; returns every error to fix: content past the "
            "slide or in the footer band, clipped text, missing ids, headlines that are too long."
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
                        "path": {
                            "type": "string",
                            "description": "The .deck.html file in your workspace.",
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }


class DeckThemesTool(Tool):
    """List the deck theme dictionary; dispatched to the runner (no file access needed)."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return "deck_themes"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List the deck themes: id, name, mood, category (professional, editorial, bold, "
            "dark), light or dark mode, and what each is best for. Use it when you choose a "
            "theme yourself, instead of remembered ids."
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


class DeckThemeSetTool(Tool):
    """Restyle a deck file with another theme, slides untouched; dispatched to the runner."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return "deck_theme_set"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Switch a deck file to another theme, only when the person asked for a new look: "
            "swaps colours, type and fonts, and keeps every slide and any CSS after the theme "
            "markers. Returns the change, or fails without touching the file when the theme "
            "block was edited by hand; then run deck_check and save again with artifact_save."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        from omnigent.superchat.decks import kit

        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The .deck.html file in your workspace.",
                        },
                        "theme_id": {"type": "string", "enum": list(kit.templates())},
                    },
                    "required": ["path", "theme_id"],
                    "additionalProperties": False,
                },
            },
        }
