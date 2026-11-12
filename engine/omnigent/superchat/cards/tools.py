"""Built-in ``render_card`` tool: show the person a structured card instead of prose.

The model *decides* when a card reads better than text (numbers to compare, a quote, sources
it read). The tool validates the input
against one small JSON schema per card kind and returns a typed ``{"type": "card", ...}``
payload that clients render with their own component catalog; the required ``fallback``
markdown rides along so the model's history (and any client without the catalog) has text.

The runner dispatches this locally (``_execute_card_tool``); no server round-trip.

Two thin sibling tools reuse the same card path so the person's reply always travels as an
ordinary chat message (no new channel): ``ask_clarification`` builds an ``ask`` card (one-click
options) and ``suggest_follow_ups`` builds a ``follow_ups`` card (quiet next-message chips).

Portions modified from getnao/nao apps/shared/src/tools/clarification.ts and
apps/shared/src/tools/suggest-follow-ups.ts@5bde830, Apache-2.0; changes: options are 2-5
required short distinct strings (nao allows none), results are Superchat reply cards (``ask`` and
``follow_ups``) that the person's click answers as an ordinary chat message, and the Muse rules
(ask only when genuinely ambiguous, suggest only natural next steps) are in the tool descriptions;
see NOTICE.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from omnigent.harnesses.executor import ENDS_TURN_KEY
from omnigent.tools.base import Tool, ToolContext

CARD_TOOL_NAME = "render_card"

_STR: dict[str, Any] = {"type": "string"}
_NUM: dict[str, Any] = {"type": "number"}


def _obj(required: list[str], **props: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _shape(schema: dict[str, Any]) -> str:
    """Compact human-readable shape of a JSON schema, e.g. ``{items: [{title, url}]}``."""
    kind = schema.get("type")
    if kind == "object":
        required = set(schema.get("required", ()))
        fields = ", ".join(
            f"{k}{'' if k in required else '?'}: {_shape(v)}"
            for k, v in schema.get("properties", {}).items()
        )
        return "{" + fields + "}"
    if kind == "array":
        return "[" + _shape(schema["items"]) + "]"
    if "enum" in schema:
        return "|".join(schema["enum"])
    return str(kind)


def _kind_hints() -> str:
    return "; ".join(f"{kind} {_shape(s)}" for kind, s in CARD_DATA_SCHEMAS.items())


# No `chart` card is offered: charts and dashboards are HTML pages `superchat-dashboard` builds
# from the person's files, so the model never types data values. Old transcripts keep their chart
# cards; clients still draw them.

# Kinds that are the answer itself. ``progress`` is a living card the Assistant re-renders (same
# ``id``) while it works, so it never ends the reply; ``ask`` waits on the person's pick.
TERMINAL_CARDS = frozenset({"sources", "compare", "ask", "person", "file", "quote"})

#: The longest ``message`` an answer card takes: one or two sentences, never the card's content.
MESSAGE_MAX_CHARS = 400


def _arr(items: dict[str, Any], *, min_items: int = 1, max_items: int = 50) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": min_items, "maxItems": max_items}


#: The ``ask`` card is drawn only for ``ask_clarification`` (one way to ask); it is not a
#: ``render_card`` kind, though the client still draws it from older conversations.
_ASK_SCHEMA: dict[str, Any] = _obj(
    ["question", "options"],
    question=_STR,
    options=_arr(
        _obj(
            ["id", "label"],
            id=_STR,
            label=_STR,
            preview=_obj(["kind", "id"], kind=_STR, id=_STR),
        ),
        max_items=8,
    ),
    allowFreeText={"type": "boolean"},
)

# One schema per kind. These are both the model-facing schema and the validator's source.
CARD_DATA_SCHEMAS: dict[str, dict[str, Any]] = {
    "sources": _obj(
        ["items"],
        items=_arr(_obj(["title", "url"], title=_STR, url=_STR, snippet=_STR)),
    ),
    "compare": _obj(
        ["columns", "rows"],
        columns=_arr(_STR, max_items=8),
        rows=_arr(_obj(["label", "cells"], label=_STR, cells=_arr(_STR, max_items=8))),
    ),
    "quote": _obj(
        ["symbol", "price"],
        symbol=_STR,
        name=_STR,
        price=_NUM,
        currency=_STR,
        change=_NUM,
        changePct=_NUM,
        asOf=_STR,
        source=_STR,
    ),
    "person": _obj(["name"], name=_STR, role=_STR, org=_STR, email=_STR, phone=_STR, url=_STR),
    "file": _obj(
        ["name"],
        name=_STR,
        url=_STR,
        kind=_STR,
        size=_NUM,
        artifactId=_STR,
        version=_NUM,
        versions=_NUM,
    ),
    "progress": _obj(
        ["label", "value", "status"],
        label=_STR,
        value={"type": "number", "minimum": 0, "maximum": 100},
        status={"type": "string", "enum": ["running", "done", "failed"]},
    ),
}

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _check(value: Any, schema: dict[str, Any], path: str) -> str | None:
    """Return the first violation of the supported JSON-schema subset, or ``None``."""
    kind = schema["type"]
    expected = _JSON_TYPES[kind]
    if not isinstance(value, expected) or (kind == "number" and isinstance(value, bool)):
        return f"{path} must be a {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {', '.join(schema['enum'])}"
    if kind == "number":
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} must be <= {schema['maximum']}"
    if kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            return f"{path} needs {schema['minItems']}-{schema['maxItems']} items"
        for i, item in enumerate(value):
            if (err := _check(item, schema["items"], f"{path}[{i}]")) is not None:
                return err
    if kind == "object":
        props = schema["properties"]
        for key in schema["required"]:
            if key not in value:
                return f"{path}.{key} is required"
        for key, item in value.items():
            if key not in props:
                return f"{path}.{key} is not allowed"
            if (err := _check(item, props[key], f"{path}.{key}")) is not None:
                return err
    return None


def card_catalog() -> list[dict[str, Any]]:
    """
    The render_card catalog: every card kind with the JSON schema of its ``data``.

    :returns: ``[{"kind", "ends_reply", "data_schema"}]`` in catalog order; ``ends_reply`` is
        true for answer cards and false for living cards (progress) updated in place.
    """
    return [
        {"kind": kind, "ends_reply": kind in TERMINAL_CARDS, "data_schema": schema}
        for kind, schema in CARD_DATA_SCHEMAS.items()
    ]


def build_card(args: dict[str, Any]) -> dict[str, Any]:
    """
    Validate ``render_card`` arguments and build the typed result payload.

    :param args: Parsed tool arguments.
    :returns: ``{"type": "card", "card", "id"?, "title"?, "message"?, "data", "fallback"}``, or
        ``{"error": "..."}`` describing the first problem so the model can retry. ``message`` (the
        words shown above an answer card) is kept for terminal cards only.
    """
    return _build_card(args, CARD_DATA_SCHEMAS)


def _build_card(args: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """:func:`build_card` over the given kinds (``ask_clarification`` adds the ``ask`` kind)."""
    card = args.get("card")
    if card not in schemas:
        return {"error": f"card must be one of {', '.join(schemas)}"}
    fallback = args.get("fallback")
    if not isinstance(fallback, str) or not fallback.strip():
        return {"error": "fallback must be a non-empty markdown string"}
    for key in ("id", "title", "message"):
        if key in args and not isinstance(args[key], str):
            return {"error": f"{key} must be a string"}
    message = " ".join(str(args.get("message") or "").split())
    if len(message) > MESSAGE_MAX_CHARS:
        return {"error": f"message is over {MESSAGE_MAX_CHARS} characters; one or two sentences"}
    err = _check(args.get("data"), schemas[card], "data")
    if err is not None:
        return {"error": err}
    out: dict[str, Any] = {"type": "card", "card": card}
    out.update({key: args[key] for key in ("id", "title") if args.get(key)})
    if message and card in TERMINAL_CARDS:
        out["message"] = message
    out["data"] = args["data"]
    out["fallback"] = fallback.strip()
    # The model reacts to a tool result; without this it adds a line repeating the card.
    # Progress cards are updated in place mid-work, so they must not stop the turn.
    if card in TERMINAL_CARDS:
        out["note"] = (
            "Shown to the person. Add no more text; only `suggest_follow_ups` may still follow, "
            "then end your turn."
        )
    else:
        out["note"] = "Shown to the person. Do not repeat it in text; carry on with the work."
    return out


class RenderCardTool(Tool):
    """Show the person one structured card; the runner dispatches it locally."""

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return CARD_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Show the person a card instead of prose, only when it reads better than text: "
            "numbers to compare (compare), a market quote (quote), "
            "sources you read (sources), a contact "
            "(person), a file (file), long-running work (progress). For a chart or a "
            "dashboard build a page with `superchat-dashboard` from the data files, never a card "
            "with typed numbers. "
            "Otherwise write normally. `data` is an object whose shape depends on `card` "
            f"(`?` = optional): {_kind_hints()}. "
            "`fallback` is a complete markdown summary of the card, used where it cannot be "
            "drawn and kept in your history. Every card but progress needs `message`: one or two "
            "plain sentences for the person (what was made, anything still open), shown above "
            "the card; write no other text around it and never repeat the card's content "
            "(only `suggest_follow_ups` may follow)."
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
                        "card": {"type": "string", "enum": list(CARD_DATA_SCHEMAS)},
                        "id": {
                            "type": "string",
                            "description": "Stable id; reuse it to update the card in place.",
                        },
                        "title": {"type": "string", "description": "Optional short heading."},
                        "message": {
                            "type": "string",
                            "maxLength": MESSAGE_MAX_CHARS,
                            "description": (
                                "Required except for progress: one or two plain sentences for "
                                "the person, shown above the card."
                            ),
                        },
                        "data": {
                            "type": "object",
                            "description": "Card payload; shapes are in the tool description.",
                        },
                        "fallback": {
                            "type": "string",
                            "description": "Markdown summary of the whole card.",
                        },
                    },
                    "required": ["card", "data", "fallback"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Validate *arguments* and return the card payload as JSON.

        :param arguments: JSON-encoded arguments from the LLM.
        :param ctx: Unused.
        :returns: JSON string: the card payload, or ``{"error": ...}``.
        """
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            return json.dumps({"error": "arguments must be a JSON object"})
        if not isinstance(args, dict):
            return json.dumps({"error": "arguments must be a JSON object"})
        return json.dumps(build_card(args), ensure_ascii=False)


# ── ask_clarification / suggest_follow_ups ──────────────────────────────────────────────────

CLARIFICATION_TOOL_NAME = "ask_clarification"
FOLLOW_UPS_TOOL_NAME = "suggest_follow_ups"

CLARIFICATION_MIN_OPTIONS = 2
CLARIFICATION_MAX_OPTIONS = 5
CLARIFICATION_OPTION_MAX_CHARS = 60
FOLLOW_UPS_MAX = 3
FOLLOW_UP_MAX_CHARS = 120


def _clean_strings(
    value: Any, *, path: str, min_items: int, max_items: int, max_chars: int
) -> tuple[list[str], str | None]:
    """Trimmed, distinct strings of *value*, or the first problem with it."""
    if not isinstance(value, list):
        return [], f"{path} must be an array of strings"
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            return [], f"{path}[{i}] must be a string"
        # Zero-width and bidi controls (category Cf) can hide or reorder what a button says.
        text = " ".join("".join(c for c in item if unicodedata.category(c) != "Cf").split())
        if not text:
            return [], f"{path}[{i}] must not be empty"
        if len(text) > max_chars:
            return [], f"{path}[{i}] is over {max_chars} characters; shorten it"
        if text.lower() in {seen.lower() for seen in out}:
            return [], f"{path}[{i}] repeats another entry"
        out.append(text)
    if not min_items <= len(out) <= max_items:
        return [], f"{path} needs {min_items}-{max_items} items"
    return out, None


#: What an option's ``preview`` may point at: ``kind`` -> the ids that exist. A capability adds
#: its kind with :func:`register_preview_kind` (decks: ``deck-theme``); the client draws the
#: preview from its own data. An unknown kind or id is refused.
PREVIEW_KINDS: dict[str, Callable[[], set[str]]] = {}
#: ``kind`` -> a check of the ids one question offers together: the problem with the set, or None.
PREVIEW_CHECKS: dict[str, Callable[[list[str]], str | None]] = {}


def register_preview_kind(
    kind: str,
    ids: Callable[[], set[str]],
    check: Callable[[list[str]], str | None] | None = None,
) -> None:
    """Allow ``{"kind": kind, "id": <one of ids()>}`` as an ask option's preview.

    :param check: Refuses a set of ids offered together (e.g. near-identical choices).
    """
    PREVIEW_KINDS[kind] = ids
    if check is not None:
        PREVIEW_CHECKS[kind] = check


#: What a model-given option id may be (the web sends it back with the pick).
_OPTION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _split_options(raw: Any) -> tuple[list[Any], list[Any], list[Any], str | None]:
    """Labels, previews and ids of ``options`` (each a string or ``{id?, label, preview?}``)."""
    if not isinstance(raw, list):
        return [], [], [], "options must be an array"
    labels: list[Any] = []
    previews: list[Any] = []
    given_ids: list[Any] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            labels.append(item)
            previews.append(None)
            given_ids.append(None)
            continue
        if set(item) - {"id", "label", "preview"}:
            return [], [], [], f"options[{i}] allows only id, label and preview"
        option_id = item.get("id")
        if option_id is not None and (
            not isinstance(option_id, str) or not _OPTION_ID.fullmatch(option_id)
        ):
            return [], [], [], f"options[{i}].id must be 1-64 letters, digits, - or _"
        preview = item.get("preview")
        if preview is not None:
            if not isinstance(preview, dict) or set(preview) != {"kind", "id"}:
                return [], [], [], f"options[{i}].preview must be {{kind, id}}"
            ids = PREVIEW_KINDS.get(preview["kind"]) if isinstance(preview["kind"], str) else None
            if ids is None:
                return (
                    [],
                    [],
                    [],
                    f"options[{i}].preview.kind must be one of {', '.join(PREVIEW_KINDS)}",
                )
            if preview["id"] not in ids():
                return [], [], [], f"options[{i}].preview.id is not a known {preview['kind']}"
        labels.append(item.get("label"))
        previews.append(preview)
        given_ids.append(option_id)
    for kind, check in PREVIEW_CHECKS.items():
        problem = check([p["id"] for p in previews if p and p["kind"] == kind])
        if problem:
            return [], [], [], problem
    return labels, previews, given_ids, None


def _option_ids(
    given: list[Any], labels: list[str], previews: list[Any]
) -> tuple[list[str], str | None]:
    """Each option's id: its preview's id (a ``deck-theme`` option is its theme id), else the
    model's, else one made from its label; all distinct. The pick comes back by this id."""
    out: list[str] = []
    for i, (option_id, label, preview) in enumerate(zip(given, labels, previews, strict=True)):
        if preview:
            option_id = preview["id"]
        if option_id is None:
            base = "-".join(re.findall(r"[a-z0-9]+", label.lower()))[:56] or f"opt-{i + 1}"
            option_id, n = base, 2
            while option_id in out or option_id in given:
                option_id, n = f"{base}-{n}", n + 1
        elif option_id in out:
            return [], f"options[{i}].id repeats another option's id"
        out.append(option_id)
    return out, None


def build_clarification(
    args: dict[str, Any], round_id: str | None = None, card_id: str | None = None
) -> dict[str, Any]:
    """
    Validate ``ask_clarification`` arguments and build its ``ask`` card payload.

    :param args: Parsed tool arguments: ``question`` and 2-5 short, distinct ``options``.
    :param round_id: The Round the card asks in (ADR 0029), kept in the stored payload.
    :param card_id: The engine's id for the card: the transcript's ``card_id``, which the web
        sends back with the pick.
    :returns: A ``{"type": "card", "card": "ask", ...}`` payload shaped like ``render_card``'s,
        or ``{"error": "..."}`` so the model can retry.
    """
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return {"error": "question must be a non-empty string"}
    question = " ".join(question.split())
    raw_labels, previews, given_ids, err = _split_options(args.get("options"))
    if err is not None:
        return {"error": err}
    options, err = _clean_strings(
        raw_labels,
        path="options",
        min_items=CLARIFICATION_MIN_OPTIONS,
        max_items=CLARIFICATION_MAX_OPTIONS,
        max_chars=CLARIFICATION_OPTION_MAX_CHARS,
    )
    if err is not None:
        return {"error": err}
    ids, err = _option_ids(given_ids, options, previews)
    if err is not None:
        return {"error": err}
    fallback = question + "\n\n" + "\n".join(f"- {option}" for option in options)
    out = _build_card(
        {
            "card": "ask",
            "data": {
                "question": question,
                "options": [
                    {"id": i, "label": o, **({"preview": p} if p else {})}
                    for i, o, p in zip(ids, options, previews, strict=True)
                ],
            },
            "fallback": fallback,
            **({"id": card_id} if card_id else {}),
        },
        {"ask": _ASK_SCHEMA},
    )
    if "error" not in out:
        out["note"] = (
            "Shown to the person with one button per option. Your turn ends here; their pick "
            "arrives as their next message."
        )
        out[ENDS_TURN_KEY] = True
        if round_id:
            out["round_id"] = round_id
    return out


def build_follow_ups(args: dict[str, Any]) -> dict[str, Any]:
    """
    Validate ``suggest_follow_ups`` arguments and build its ``follow_ups`` card payload.

    :param args: Parsed tool arguments: 1-3 short ``suggestions``.
    :returns: ``{"type": "card", "card": "follow_ups", "data", "fallback", "note"}``, or
        ``{"error": "..."}`` so the model can retry.
    """
    suggestions, err = _clean_strings(
        args.get("suggestions"),
        path="suggestions",
        min_items=1,
        max_items=FOLLOW_UPS_MAX,
        max_chars=FOLLOW_UP_MAX_CHARS,
    )
    if err is not None:
        return {"error": err}
    return {
        "type": "card",
        "card": "follow_ups",
        "data": {"suggestions": suggestions},
        "fallback": "\n".join(f"- {s}" for s in suggestions),
        "note": "Shown as quiet chips under your answer.",
    }


class _CardSiblingTool(Tool):
    """Shared plumbing of the two sibling tools: parse JSON, build, return JSON."""

    _build: Any = None

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Validate *arguments* and return the card payload as JSON.

        :param arguments: JSON-encoded arguments from the LLM.
        :param ctx: Unused.
        :returns: JSON string: the card payload, or ``{"error": ...}``.
        """
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            return json.dumps({"error": "arguments must be a JSON object"})
        if not isinstance(args, dict):
            return json.dumps({"error": "arguments must be a JSON object"})
        return json.dumps(type(self)._build(args), ensure_ascii=False)


class AskClarificationTool(_CardSiblingTool):
    """Ask the person one clarifying question with 2-5 one-click options."""

    _build = staticmethod(build_clarification)

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return CLARIFICATION_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Ask the person one clarifying question as a card with 2-5 one-click options. Use "
            "it only when the request is genuinely ambiguous, a wrong guess would waste real "
            "work, and you can name the options clearly. If a sensible default exists, or the "
            "answer is cheap to correct afterwards, do not ask: just proceed and say what you "
            "assumed. One question per call, options short, distinct and mutually exclusive. "
            "Their pick comes back as their next message; the card ends your reply."
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
                        "question": {
                            "type": "string",
                            "description": "One short, specific question.",
                        },
                        "options": {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "id": {
                                                "type": "string",
                                                "description": "Stable option id (optional).",
                                            },
                                            "label": {"type": "string"},
                                            "preview": {
                                                "type": "object",
                                                "properties": {
                                                    "kind": {
                                                        "type": "string",
                                                        "enum": list(PREVIEW_KINDS),
                                                    },
                                                    "id": {"type": "string"},
                                                },
                                                "required": ["kind", "id"],
                                                "additionalProperties": False,
                                            },
                                        },
                                        "required": ["label"],
                                        "additionalProperties": False,
                                    },
                                ]
                            },
                            "minItems": CLARIFICATION_MIN_OPTIONS,
                            "maxItems": CLARIFICATION_MAX_OPTIONS,
                            "description": (
                                f"{CLARIFICATION_MIN_OPTIONS}-{CLARIFICATION_MAX_OPTIONS} "
                                f"distinct answers, each under "
                                f"{CLARIFICATION_OPTION_MAX_CHARS} characters. An option may be "
                                '{"label", "preview": {"kind": "deck-theme", "id": <deck '
                                "theme id>}} to show that theme's thumbnail."
                            ),
                        },
                    },
                    "required": ["question", "options"],
                    "additionalProperties": False,
                },
            },
        }


class SuggestFollowUpsTool(_CardSiblingTool):
    """Offer 1-3 natural next messages as quiet chips under the answer."""

    _build = staticmethod(build_follow_ups)

    @classmethod
    def name(cls) -> str:
        """:returns: The tool name."""
        return FOLLOW_UPS_TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Offer 1-3 short messages the person could send next, in their voice and under 120 "
            "characters each; they are shown as quiet chips under your answer. Use it only for "
            'natural next steps after a full answer, never for filler ("Anything else?") and '
            "never together with ask_clarification."
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
                        "suggestions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": FOLLOW_UPS_MAX,
                            "description": "1-3 next messages, in the person's voice.",
                        }
                    },
                    "required": ["suggestions"],
                    "additionalProperties": False,
                },
            },
        }
