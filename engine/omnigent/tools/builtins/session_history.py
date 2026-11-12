"""Built-in tool: read-only recall over the calling session and its chats.

The rollover super chat compacts older messages into a summary — "the
summary is a pointer, not the truth" (``docs/context.md``). This tool
lets the model page backward through, or full-text search, the exact items
that summary was built from, check how much context headroom is left, and
read a related chat by ``chat_id``: from a scheduled or ad-hoc Helper, the
Conversation it works for (``list_chats``); from any chat, a Helper it started.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

# Mirrors ``_ACTIVITY_MAX_CHARS`` in ``spawn.py``, the compact-preview budget.
_CONTENT_MAX_CHARS = 2000
# A tool call's arguments and a tool's result are read as a short digest: the words of the
# person and the assistant are what a recall is for, and bulk read here stays in context.
_TOOL_MAX_CHARS = 300

_READ_DEFAULT_LIMIT = 5
_READ_MAX_LIMIT = 20
# One ``read`` returns about this many characters at most (the newest-first or oldest-first
# turns that fit, always at least one); the rest is a next_cursor away.
_READ_MAX_CHARS = 12_000
_READ_ORDERS = frozenset({"newest", "oldest"})

# Safety cap on total items scanned while grouping into turns, across
# batches, so a conversation with an unusually long tool-call run (or
# malformed data with no user-message boundaries) can't loop unbounded.
_READ_MAX_ITEMS_SCANNED = 2000
_READ_BATCH_SIZE = 100

_SEARCH_DEFAULT_LIMIT = 10
_SEARCH_MAX_LIMIT = 20

# Item kinds recall returns; everything else (notably reasoning) is hidden.
_RECALL_ITEM_KINDS = frozenset({"message", "function_call", "function_call_output"})
_HIDDEN_TYPE = "hidden"

_ACTIONS = frozenset({"get", "list_chats", "read", "search", "status"})


class SessionHistoryTool(Tool):
    """
    Page, search, and check token headroom for the calling session, and
    look into a chat it may read (a Helper's parent Conversation).

    Scoped by construction: the session id always comes from
    :attr:`ToolContext.conversation_id` (set by the runtime from the turn
    that invoked the tool), never from the model-supplied arguments.
    ``read``/``search`` accept a ``chat_id`` to target a different chat, but
    it is validated against ``ctx.conversation_id``'s own ``list_chats`` set
    before use (see :func:`_resolve_chat_target`) — there is still no
    argument shape that reaches an unrelated session's record. Read-only:
    no action writes or mutates anything, in this chat or any other.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"session_history"``."""
        return "session_history"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "The exact record of this chat; what you see of earlier turns may be shortened. "
            "Call it before answering about earlier messages (first message, exact wording, "
            "what was decided). Read-only. "
            "action='read': turns (a user message and everything that answered it; tool items "
            "shortened), newest first, or from the start with order='oldest' (the first "
            "message: order='oldest', limit=1). limit caps turns per call (default 5, maximum "
            f"20); a call returns about {_READ_MAX_CHARS} characters at most. Pass next_cursor "
            "as cursor, with the same order, to read the next page. "
            "action='get': one item unclipped, by call_id or item_id (neither: the chat's last "
            f"reply), {_READ_MAX_CHARS} characters at a time from offset; next_offset says "
            "where the rest starts. "
            "action='search': full-text search. "
            "action='status': tokens used, the context window and tokens left before the next "
            "rollover. "
            "action='list_chats': the other chats you may read (a Helper's parent "
            "conversation)."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": SessionHistoryTool.name(),
                "description": SessionHistoryTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": sorted(_ACTIONS)},
                        "cursor": {
                            "type": "string",
                            "description": "read only. next_cursor from the previous call.",
                        },
                        "order": {"type": "string", "enum": sorted(_READ_ORDERS)},
                        "around": {
                            "type": "string",
                            "description": (
                                "read only. An item id: read from the turn that holds it, "
                                "oldest first."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                f"read: turns, default {_READ_DEFAULT_LIMIT}, max "
                                f"{_READ_MAX_LIMIT}. search: results, default "
                                f"{_SEARCH_DEFAULT_LIMIT}, max {_SEARCH_MAX_LIMIT}."
                            ),
                        },
                        "query": {"type": "string", "description": "search only."},
                        "call_id": {
                            "type": "string",
                            "description": "get only. A tool call's id: returns its result.",
                        },
                        "item_id": {"type": "string", "description": "get only."},
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "get only. First character (default 0).",
                        },
                        "length": {
                            "type": "integer",
                            "minimum": 1,
                            "description": f"get only. Characters, up to {_READ_MAX_CHARS}.",
                        },
                        "chat_id": {
                            "type": "string",
                            "description": (
                                "get/read/search: another chat, by an id from list_chats or "
                                "a Helper's id from its result. Omit for this chat."
                            ),
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Dispatch to the requested action, scoped to ``ctx.conversation_id``.

        :param arguments: JSON-encoded arguments from the LLM, e.g.
            ``{"action": "read", "cursor": "item_abc123"}``.
        :param ctx: Server-side execution context. ``ctx.conversation_id``
            is the ONLY source of SCOPE — ``read``/``search`` may target a
            different chat via ``chat_id``, but only one already in
            ``ctx.conversation_id``'s own ``list_chats`` set (see
            :func:`_resolve_chat_target`); never trusted from *arguments*
            alone.
        :returns: JSON string result, or ``{"error": ...}`` on failure.
        """
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        if not ctx.conversation_id:
            return json.dumps({"error": "session_history has no calling session in context"})

        action = args.get("action")
        if action not in _ACTIONS:
            return json.dumps({"error": f"action must be one of {sorted(_ACTIONS)}"})

        from omnigent.runtime import get_conversation_store

        conv_store = get_conversation_store()
        if action == "list_chats":
            return _list_chats(conv_store, ctx.conversation_id)
        if action in ("get", "read", "search"):
            target_id, error = _resolve_chat_target(conv_store, ctx.conversation_id, args)
            if error is not None:
                return error
            assert target_id is not None
            if action == "get":
                return _get(conv_store, target_id, args)
            if action == "read":
                return _read(conv_store, target_id, args)
            return _search(conv_store, target_id, args)
        return _status(conv_store, ctx.conversation_id)


def _list_chats(conv_store: Any, conversation_id: str) -> str:
    """The ``list_chats`` action's JSON payload."""
    from omnigent.context.rollover import list_related_chats

    return json.dumps({"chats": list_related_chats(conv_store, conversation_id)})


def _resolve_chat_target(
    conv_store: Any, conversation_id: str, args: dict[str, Any]
) -> tuple[str | None, str | None]:
    """
    Resolve the session ``read``/``search`` actually targets.

    ``chat_id`` is optional and defaults to *conversation_id* (the calling
    session itself). When set to something else, it must be one of
    *conversation_id*'s own ``list_chats`` or a Helper it started, owned by the
    same person — never trusted on its own, the same context-not-argument
    scoping the rest of this tool uses.

    :returns: ``(target_conversation_id, None)`` on success, or
        ``(None, json_error_string)``.
    """
    chat_id = args.get("chat_id")
    if chat_id is None:
        return conversation_id, None
    if not isinstance(chat_id, str) or not chat_id:
        return None, json.dumps({"error": "chat_id must be a non-empty string"})
    if chat_id == conversation_id:
        return chat_id, None

    from omnigent.context.rollover import related_chat_ids

    if chat_id not in related_chat_ids(conv_store, conversation_id, chat_id):
        return None, json.dumps(
            {"error": "chat_id is not a related chat of this session", "chat_id": chat_id}
        )
    return chat_id, None


def clamp_limit(raw: Any, *, default: int, maximum: int) -> int | str:
    """Coerce + clamp a ``limit`` argument to ``[1, maximum]``, default when absent."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        return json.dumps({"error": f"limit must be an integer, got {raw!r}"})
    if raw < 1:
        return json.dumps({"error": "limit must be >= 1"})
    return min(raw, maximum)


def _read(conv_store: Any, conversation_id: str, args: dict[str, Any]) -> str:
    """Read full turns backward from ``cursor`` (or the newest), newest turn first."""
    cursor = args.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        return json.dumps({"error": "cursor must be a string"})
    limit = clamp_limit(args.get("limit"), default=_READ_DEFAULT_LIMIT, maximum=_READ_MAX_LIMIT)
    if isinstance(limit, str):
        return limit
    oldest = read_order(args.get("order"))
    if oldest is None:
        return json.dumps({"error": f"order must be one of {sorted(_READ_ORDERS)}"})

    from omnigent.errors import StaleCursorError

    around = args.get("around")
    if around is not None:
        if not isinstance(around, str) or not around:
            return json.dumps({"error": "around must be an item id"})
        try:
            newer = conv_store.list_items(conversation_id, limit=1, after=around, order="asc")
            back = conv_store.list_items(
                conversation_id,
                limit=_READ_BATCH_SIZE,
                after=newer.data[0].id if newer.data else None,
                order="desc",
            )
        except StaleCursorError:
            return json.dumps({"error": "stale_cursor", "around": around})
        cursor = around_cursor([_project_item(item) for item in back.data], back.has_more)
        oldest = True

    def fetch_page(cursor_item_id: str | None) -> tuple[list[dict[str, Any]], bool]:
        # ``after`` (not ``before``) is correct here: it means "further in sort
        # direction" — older in desc order, newer in asc (see
        # ConversationStore.list_items).
        page = conv_store.list_items(
            conversation_id,
            limit=_READ_BATCH_SIZE,
            after=cursor_item_id,
            order="asc" if oldest else "desc",
        )
        return [_project_item(item) for item in page.data], page.has_more

    try:
        turns, next_cursor = group_into_turns(
            fetch_page, limit=limit, start_cursor=cursor, oldest_first=oldest
        )
    except StaleCursorError:
        return json.dumps({"error": "stale_cursor", "cursor": cursor})

    return read_response(turns, next_cursor, oldest_first=oldest)


def get_window(args: dict[str, Any]) -> tuple[str | None, str | None, int, int] | str:
    """Validate ``get`` arguments: ``(call_id, item_id, offset, length)``, or an error JSON."""
    call_id, item_id = args.get("call_id"), args.get("item_id")
    if any(v is not None and not isinstance(v, str) for v in (call_id, item_id)):
        return json.dumps({"error": "call_id and item_id must be strings"})
    offset = args.get("offset", 0)
    length = args.get("length", _READ_MAX_CHARS)
    for name, value, floor in (("offset", offset, 0), ("length", length, 1)):
        if isinstance(value, bool) or not isinstance(value, int) or value < floor:
            return json.dumps({"error": f"{name} must be an integer >= {floor}"})
    return call_id or None, item_id or None, offset, min(length, _READ_MAX_CHARS)


def is_requested_item(item: dict[str, Any], call_id: str | None, item_id: str | None) -> bool:
    """Whether a flat API item is the one ``get`` asked for; with no id, the last reply."""
    if call_id is None and item_id is None:
        return item.get("type") == "message" and item.get("role") == "assistant"
    if item_id is not None and item.get("id") == item_id:
        return True
    return (
        call_id is not None
        and item.get("type") == "function_call_output"
        and item.get("call_id") == call_id
    )


def get_response(item: dict[str, Any] | None, offset: int, length: int) -> str:
    """The ``get`` action's JSON payload: the item's text from *offset*, unclipped."""
    if item is None:
        return json.dumps({"error": "no such item in the scanned part of the record"})
    if item.get("type") == "function_call_output":
        output = item.get("output")
        text = output if isinstance(output, str) else json.dumps(output)
    elif item.get("type") == "function_call":
        text = str(item.get("arguments") or "")
    else:
        text = _message_text(item)
    end = offset + length
    body: dict[str, Any] = {
        "id": item.get("id"),
        "content": text[offset:end],
        "total_chars": len(text),
    }
    if end < len(text):
        body["next_offset"] = end
    return json.dumps(body)


def _get(conv_store: Any, conversation_id: str, args: dict[str, Any]) -> str:
    """Return one stored item unclipped, found newest first among the latest items."""
    window = get_window(args)
    if isinstance(window, str):
        return window
    call_id, item_id, offset, length = window
    cursor: str | None = None
    scanned = 0
    while scanned < _READ_MAX_ITEMS_SCANNED:
        page = conv_store.list_items(
            conversation_id, limit=_READ_BATCH_SIZE, after=cursor, order="desc"
        )
        for raw in page.data:
            item = raw.to_api_dict()
            if is_requested_item(item, call_id, item_id):
                return get_response(item, offset, length)
        scanned += len(page.data)
        if not page.data or not page.has_more:
            break
        cursor = page.data[-1].id
    return get_response(None, offset, length)


def around_cursor(items: list[dict[str, Any]], has_more: bool) -> str | None:
    """The cursor an oldest-first read starts from to begin at the turn holding ``items[0]``.

    *items* are projected items, newest first: the ``around`` item, then older ones. The turn
    starts at the first user message among them; the cursor is the item just before it.
    """
    for idx, item in enumerate(items):
        if item.get("type") == "text" and item.get("role") == "user":
            if idx + 1 < len(items):
                return items[idx + 1]["id"]
            return item["id"] if has_more else None
    return items[-1]["id"] if items and has_more else None


def read_order(raw: Any) -> bool | None:
    """``True`` for ``order='oldest'``, ``False`` for newest (the default), ``None`` if invalid."""
    if raw is None:
        return False
    return raw == "oldest" if raw in _READ_ORDERS else None


class TurnCollector:
    """Group projected items into full turns, page by page.

    A turn is a user message plus everything that answered it. Pages come
    newest first, or oldest first with *oldest_first*. Collecting stops at
    *limit* turns or once the turns reach :data:`_READ_MAX_CHARS`. Shared by
    the in-process read and the runner's async REST read, which differ only
    in how they fetch a page.
    """

    def __init__(self, limit: int, *, oldest_first: bool = False) -> None:
        self._limit = limit
        self._oldest_first = oldest_first
        self.turns: list[list[dict[str, Any]]] = []
        self._current: list[dict[str, Any]] = []
        self._scanned = 0
        self._chars = 0
        self._last_id: str | None = None

    @property
    def wants_more(self) -> bool:
        return (
            len(self.turns) < self._limit
            and self._chars < _READ_MAX_CHARS
            and self._scanned < _READ_MAX_ITEMS_SCANNED
        )

    def _close(self, turn: list[dict[str, Any]]) -> bool:
        """Add a whole turn; whether the read is done."""
        self.turns.append(turn)
        self._chars += len(json.dumps(turn, ensure_ascii=False))
        return not self.wants_more

    def feed(
        self, items: list[dict[str, Any]], has_more: bool
    ) -> tuple[list[list[dict[str, Any]]], str | None] | None:
        """Add one page; return ``(turns, next_cursor)`` once done."""
        for idx, item in enumerate(items):
            self._scanned += 1
            is_turn_start = item.get("type") == "text" and item.get("role") == "user"
            if self._oldest_first:
                # A user message closes the turn before it; the next page starts at it.
                if is_turn_start and self._current and self._close(self._current):
                    self._current = []
                    return self.turns, self._last_id
                if is_turn_start:
                    self._current = []
                self._last_id = item.get("id")
                if item.get("type") != _HIDDEN_TYPE:
                    self._current.append(item)
                continue
            if item.get("type") == _HIDDEN_TYPE:
                continue
            self._current.append(item)
            if is_turn_start:
                done = self._close(list(reversed(self._current)))
                self._current = []
                if done:
                    # A cut on the page's last item only has more behind it if has_more.
                    if idx == len(items) - 1 and not has_more:
                        return self.turns, None
                    return self.turns, items[idx]["id"]
        if not has_more:
            return self.finish(None)
        return None

    def finish(self, cursor: str | None) -> tuple[list[list[dict[str, Any]]], str | None]:
        """Flush a partial turn (end of record or scan cap) rather than drop it."""
        if self._current:
            turn = self._current if self._oldest_first else list(reversed(self._current))
            self.turns.append(turn)
            self._current = []
        return self.turns, cursor


def group_into_turns(
    fetch_page: Callable[[str | None], tuple[list[dict[str, Any]], bool]],
    *,
    limit: int,
    start_cursor: str | None = None,
    oldest_first: bool = False,
) -> tuple[list[list[dict[str, Any]]], str | None]:
    """Collect up to *limit* full turns in *fetch_page*'s order, paging via *fetch_page*."""
    collector = TurnCollector(limit, oldest_first=oldest_first)
    cursor = start_cursor
    while collector.wants_more:
        items, has_more = fetch_page(cursor)
        if not items:
            break
        done = collector.feed(items, has_more)
        if done is not None:
            return done
        cursor = items[-1]["id"]
    return collector.finish(cursor)


def read_response(
    turns: list[list[dict[str, Any]]], next_cursor: str | None, *, oldest_first: bool = False
) -> str:
    """The ``read`` action's JSON payload, within :data:`_READ_MAX_CHARS`.

    Messages are always kept; tool items past the budget (one long turn) are left out and
    counted. With more to read, ``more`` says how to page on.
    """
    room = _READ_MAX_CHARS
    omitted = 0
    fitted: list[dict[str, Any]] = []
    for turn in turns:
        kept: list[dict[str, Any]] = []
        for item in turn:
            size = len(json.dumps(item, ensure_ascii=False))
            if item.get("type") in ("tool_call", "tool_result") and size > room:
                omitted += 1
                continue
            room -= size
            kept.append(item)
        fitted.append({"messages": kept})
    body: dict[str, Any] = {"turns": fitted, "next_cursor": next_cursor}
    if omitted:
        body["omitted_tool_items"] = omitted
    if next_cursor is not None:
        direction = "newer" if oldest_first else "older"
        order = "oldest" if oldest_first else "newest"
        body["more"] = (
            f"There are {direction} turns: call read again with cursor=next_cursor and "
            f"order='{order}'."
        )
    return json.dumps(body)


def _search(conv_store: Any, conversation_id: str, args: dict[str, Any]) -> str:
    """Full-text search, scoped to ``conversation_id`` only."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"error": "search requires a non-empty 'query' string"})
    limit = clamp_limit(
        args.get("limit"), default=_SEARCH_DEFAULT_LIMIT, maximum=_SEARCH_MAX_LIMIT
    )
    if isinstance(limit, str):
        return limit

    items = conv_store.search(query.strip(), conversation_id=conversation_id, limit=limit)
    results = [_project_item(item) for item in items]
    return json.dumps({"results": [r for r in results if r["type"] != _HIDDEN_TYPE]})


def _status(conv_store: Any, conversation_id: str) -> str:
    """Report tokens used, the context window, and headroom before rollover."""
    conversation = conv_store.get_conversation(conversation_id)
    labels = conversation.labels if conversation is not None else {}
    return json.dumps(status_from_labels(labels))


def status_from_labels(labels: dict[str, str]) -> dict[str, Any]:
    """
    Compute the ``status`` action's fields from a session's raw labels.

    Pure function of *labels* so both the in-process tool (labels read from
    :class:`~omnigent.entities.conversation.Conversation`) and the runner's
    REST-based relay dispatch (labels read from ``GET .../labels``) share
    one implementation — see ``omnigent.runner.tool_dispatch``.

    :param labels: The session's label mapping (possibly empty).
    :returns: Dict with ``rollover_trigger_tokens`` always present, and
        ``context_window_tokens`` / ``current_context_tokens`` / ``source``
        / ``tokens_remaining_to_rollover`` / ``context_used_percent`` only
        when they can actually be computed from known values.
    """
    from omnigent.context.rollover import (
        reported_context_tokens,
        reported_context_window,
        resolve_rollover_threshold,
    )

    current_tokens = reported_context_tokens(labels)
    context_window = reported_context_window(labels)
    threshold = resolve_rollover_threshold(labels)

    # Only report what is actually known — a harness that hasn't posted a
    # usage report yet (e.g. right after a rollover) has no current_tokens,
    # so every field derived from it is omitted rather than guessed.
    result: dict[str, Any] = {"rollover_trigger_tokens": threshold}
    if context_window is not None:
        result["context_window_tokens"] = context_window
    if current_tokens is not None:
        result["current_context_tokens"] = current_tokens
        result["source"] = "reported"
        result["tokens_remaining_to_rollover"] = max(threshold - current_tokens, 0)
        if context_window is not None:
            result["context_used_percent"] = round(100 * current_tokens / context_window, 1)
    return result


def _project_item(item: Any) -> dict[str, Any]:
    """Project a :class:`ConversationItem` via its flat API dict."""
    return project_api_item(item.to_api_dict())


def project_api_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Project a flat API item into a compact dict with its id.

    Messages, tool calls and tool results collapse to role/type/content, each
    content field capped at ``_CONTENT_MAX_CHARS``.

    :param item: A ``ConversationItem.to_api_dict()`` dict.
    :returns: A compact dict with ``id``, ``created_at``, ``role``, ``type``
        and content.
    """
    base = {"id": item.get("id"), "created_at": item.get("created_at")}
    kind = item.get("type")
    if kind not in _RECALL_ITEM_KINDS:
        # Reasoning, errors, lifecycle and checkpoint items are not recalled.
        return {**base, "type": _HIDDEN_TYPE}
    if kind == "function_call":
        return {
            **base,
            "role": "assistant",
            "type": "tool_call",
            "name": item.get("name"),
            "args": _truncate(str(item.get("arguments") or ""), max_chars=_TOOL_MAX_CHARS),
        }
    if kind == "function_call_output":
        output = item.get("output")
        return {
            **base,
            "role": "tool",
            "type": "tool_result",
            "name": item.get("name"),
            "content": _truncate(
                output if isinstance(output, str) else json.dumps(output),
                max_chars=_TOOL_MAX_CHARS,
            ),
        }
    return {
        **base,
        "role": item.get("role") or "unknown",
        "type": "text",
        "content": _truncate(_message_text(item)),
    }


def _message_text(item: dict[str, Any]) -> str:
    """A message item's text blocks, joined."""
    text_parts: list[str] = []
    for block in item.get("content") or []:
        if isinstance(block, dict):
            text = block.get("text") or block.get("output_text")
            if text:
                text_parts.append(text)
        elif isinstance(block, str):
            text_parts.append(block)
    return "\n".join(text_parts)


def _truncate(text: str, *, max_chars: int = _CONTENT_MAX_CHARS) -> str:
    """Return ``text`` capped at ``max_chars``, with a truncation marker when clipped."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " [truncated]"
