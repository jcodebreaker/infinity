"""The window: a Conversation's history for a harness, rendered from the record.

A pure function of the record. An epoch starts with its stored ``epoch_block`` text, then every
item after the view start in delivery order, each as the model saw it: a turn's opening message
with the ``context_prefix`` blocks it was sent with, a steer after the tool batch it was delivered
behind (``delivered_after_call_id``), assistant text and tool calls and results as stored. Only
images are inexact: inline within the last :data:`RECENT_ROUNDS` Rounds, a named placeholder
before. Harness adapters turn the messages into their own session format.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict

from omnigent.context.rollover import _ATTACHMENT_TOKENS, _block_text, _tokens
from omnigent.entities.conversation import is_system_notice_text

#: A steer the harness took before any tool result of its turn.
TURN_START = "turn_start"
#: The result a tool call that never got one is given (the turn was stopped mid-call).
INTERRUPTED_RESULT = "interrupted, effect unknown"
#: Rounds, newest first, whose images stay inline; older ones become a named placeholder.
RECENT_ROUNDS = 2

_FILE_BLOCKS = frozenset({"input_image", "input_file"})
_TEXT_BLOCKS = frozenset({"input_text", "output_text", "text"})


class ToolCall(TypedDict):
    id: str
    name: str
    arguments: dict[str, Any]


class UserMessage(TypedDict):
    role: Literal["user"]
    #: Responses input blocks: ``input_text``, and ``input_image``/``input_file`` as data URIs.
    content: list[dict[str, Any]]


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    text: str
    tool_calls: list[ToolCall]


class ToolResult(TypedDict):
    role: Literal["tool"]
    call_id: str
    name: str
    output: str


WindowMessage = UserMessage | AssistantMessage | ToolResult
RespawnBranch = Literal["reuse", "render", "new_epoch"]


def _is_user(item: Mapping[str, Any]) -> bool:
    return item.get("type") == "message" and item.get("role", "user") == "user"


def _is_output(item: Mapping[str, Any]) -> bool:
    kind = item.get("type")
    if kind == "message":
        return item.get("role") == "assistant"
    return kind in ("function_call", "function_call_output", "reasoning")


def _steer_call(item: Mapping[str, Any]) -> str | None:
    """The call a steer was delivered after (``None`` for anything else, a turn-start steer too:
    it stays where the record put it)."""
    after = item.get("delivered_after_call_id")
    return after if _is_user(item) and isinstance(after, str) and after != TURN_START else None


def _is_steer(item: Mapping[str, Any]) -> bool:
    return _is_user(item) and item.get("delivered_after_call_id") is not None


def delivery_order(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """*items* (content items only) in the order the model got them.

    A message persisted while a turn ran sits among that turn's outputs in the record, yet was
    delivered after them (as the next turn, or as a turn-start steer): it moves behind the last
    output of the turn it landed in. Steers delivered after a call stay where they are; the
    renderer places them by their call.
    """
    last_output: dict[str, int] = {}
    for index, item in enumerate(items):
        if _is_output(item):
            last_output[str(item.get("response_id"))] = index
    keyed: list[tuple[int, int, int]] = []
    running: str | None = None
    for index, item in enumerate(items):
        if _is_output(item):
            running = str(item.get("response_id"))
        at = index
        if _is_user(item) and _steer_call(item) is None and running is not None:
            at = max(index, last_output.get(running, index))
        keyed.append((at, 0 if at == index else 1, index))
    return [items[index] for _, _, index in sorted(keyed)]


def _openers(ordered: Sequence[Mapping[str, Any]]) -> list[int]:
    """Indexes of the messages that opened a turn: a person's message, a notice or a wake that
    the next output answered (not a steer, not replayed meta context)."""
    found: list[int] = []
    for index, item in enumerate(ordered):
        if not _is_user(item) or _is_steer(item) or item.get("is_meta"):
            continue
        nxt = next((it for it in ordered[index + 1 :] if not _is_steer(it)), None)
        if nxt is not None and _is_output(nxt):
            found.append(index)
    return found


_CONTENT_TYPES = frozenset({"message", "function_call", "function_call_output", "reasoning"})


def _content_items(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [it for it in items if it.get("type") in _CONTENT_TYPES]


def pending_message(items: Sequence[Mapping[str, Any]]) -> str | None:
    """The id of the message the next turn answers: the newest item in delivery order when it is
    a message no output followed yet (not a steer). The turn sends it; the window leaves it out."""
    ordered = delivery_order(_content_items(items))
    last = ordered[-1] if ordered else None
    if last is None or not _is_user(last) or _is_steer(last) or not last.get("id"):
        return None
    return str(last["id"])


def exactly_renderable(epoch_block_text: str | None, items: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the epoch renders exactly as the model saw it: its block is stored and every
    turn-opening message says what was sent in front of it (``[]`` counts, ``None`` does not)."""
    if epoch_block_text is None:
        return False
    ordered = delivery_order(_content_items(items))
    return all(ordered[i].get("context_prefix") is not None for i in _openers(ordered))


def _round_starts(ordered: Sequence[Mapping[str, Any]]) -> list[int]:
    return [
        index
        for index, item in enumerate(ordered)
        if _is_user(item)
        and not _is_steer(item)
        and not item.get("is_meta")
        and not item.get("is_system_notice")
        and not is_system_notice_text(_block_text(item.get("content")))
    ]


def _recent_from(ordered: Sequence[Mapping[str, Any]]) -> int:
    rounds = _round_starts(ordered)
    return rounds[-RECENT_ROUNDS] if len(rounds) >= RECENT_ROUNDS else 0


def recent_message_ids(items: Sequence[Mapping[str, Any]]) -> set[str]:
    """Ids of the messages whose attachments the window keeps inline (the last Rounds)."""
    ordered = delivery_order(_content_items(items))
    return {
        str(it["id"]) for it in ordered[_recent_from(ordered) :] if _is_user(it) and it.get("id")
    }


def _with_prefix(blocks: list[dict[str, Any]], prefix: Sequence[str]) -> list[dict[str, Any]]:
    """*blocks* with the turn's prefix blocks in front, as the runner sent them: on the first
    text block, or as a text block of their own when the message has none."""
    head = "".join(f"{block}\n\n" for block in prefix if block)
    if not head:
        return blocks
    for index, block in enumerate(blocks):
        if block.get("type") in ("input_text", "text") and isinstance(block.get("text"), str):
            return [*blocks[:index], {**block, "text": head + block["text"]}, *blocks[index + 1 :]]
    return [{"type": "input_text", "text": head.rstrip("\n")}, *blocks]


def _user_content(item: Mapping[str, Any], *, recent: bool) -> list[dict[str, Any]]:
    raw = item.get("content")
    blocks = (
        [{"type": "input_text", "text": raw}]
        if isinstance(raw, str)
        else [dict(b) for b in raw if isinstance(b, dict)]
        if isinstance(raw, list)
        else []
    )
    prefix = item.get("context_prefix")
    if isinstance(prefix, list):
        blocks = _with_prefix(blocks, [str(b) for b in prefix])
    content: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind in _FILE_BLOCKS:
            data = block.get("image_url") if kind == "input_image" else block.get("file_data")
            if recent and isinstance(data, str) and data.startswith("data:"):
                content.append(block)
                continue
            name = block.get("filename") or block.get("file_id") or block.get("path") or "file"
            label = "image" if kind == "input_image" else "file"
            content.append({"type": "input_text", "text": f"[{label}: {name}, in the Library]"})
        elif kind in _TEXT_BLOCKS and isinstance(block.get("text"), str):
            content.append({"type": "input_text", "text": block["text"]})
    return content


def _arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else {}
    except ValueError:
        return {"input": raw}
    return parsed if isinstance(parsed, dict) else {"input": parsed}


def _output_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return _block_text(raw)
    return "" if raw is None else json.dumps(raw)


def render_window(
    epoch_block_text: str | None, items: Sequence[Mapping[str, Any]]
) -> list[WindowMessage]:
    """The epoch's window: its block as the first user message, then *items* (the record after
    the view start, record-only fields included) as the model saw them.

    Reasoning, errors and bookkeeping items are left out. A tool call with no result gets
    :data:`INTERRUPTED_RESULT` at the end of its batch, a result with no call is dropped, and a
    steer goes after the whole batch holding the call it was delivered after.
    """
    ordered = delivery_order(_content_items(items))
    recent_from = _recent_from(ordered)
    output_calls = {
        str(it.get("call_id")) for it in ordered if it.get("type") == "function_call_output"
    }
    messages: list[WindowMessage] = []
    if epoch_block_text:
        messages.append(
            {"role": "user", "content": [{"type": "input_text", "text": epoch_block_text}]}
        )
    steers: list[tuple[str, UserMessage]] = []
    pending: dict[str, str] = {}
    answered: set[str] = set()
    batch_open = False  # the last assistant message's calls can still grow

    def close_batch() -> None:
        nonlocal batch_open
        for call_id, name in pending.items():
            messages.append(
                {"role": "tool", "call_id": call_id, "name": name, "output": INTERRUPTED_RESULT}
            )
            answered.add(call_id)
        pending.clear()
        batch_open = False

    for index, item in enumerate(ordered):
        kind = item.get("type")
        if _is_user(item):
            message: UserMessage = {
                "role": "user",
                "content": _user_content(item, recent=index >= recent_from),
            }
            call = _steer_call(item)
            if call is not None and call in output_calls:
                steers.append((call, message))
                continue
            close_batch()
            messages.append(message)
        elif kind == "message":
            close_batch()
            messages.append(
                {"role": "assistant", "text": _block_text(item.get("content")), "tool_calls": []}
            )
            batch_open = True
        elif kind == "function_call":
            call_id = str(item.get("call_id") or "")
            if not call_id or call_id in pending or call_id in answered:
                continue
            last = messages[-1] if messages else None
            if not (batch_open and last is not None and last["role"] == "assistant"):
                close_batch()
                messages.append({"role": "assistant", "text": "", "tool_calls": []})
                batch_open = True
            name = str(item.get("name") or "")
            assistant = messages[-1]
            assert assistant["role"] == "assistant"
            assistant["tool_calls"].append(
                {"id": call_id, "name": name, "arguments": _arguments(item.get("arguments"))}
            )
            pending[call_id] = name
        elif kind == "function_call_output":
            call_id = str(item.get("call_id") or "")
            if call_id not in pending:
                continue
            messages.append(
                {
                    "role": "tool",
                    "call_id": call_id,
                    "name": pending.pop(call_id),
                    "output": _output_text(item.get("output")),
                }
            )
            answered.add(call_id)
            batch_open = False
    close_batch()
    return _place_steers(messages, steers)


def _place_steers(
    messages: list[WindowMessage], steers: list[tuple[str, UserMessage]]
) -> list[WindowMessage]:
    """*messages* with each steer after the run of tool results holding its call."""
    after: dict[int, list[UserMessage]] = {}
    for call_id, steer in steers:
        at = next(
            (i for i, m in enumerate(messages) if m["role"] == "tool" and m["call_id"] == call_id),
            None,
        )
        if at is None:
            continue
        while at + 1 < len(messages) and messages[at + 1]["role"] == "tool":
            at += 1
        after.setdefault(at, []).append(steer)
    placed: list[WindowMessage] = []
    for i, message in enumerate(messages):
        placed.append(message)
        placed.extend(after.get(i, []))
    return placed


def message_text(message: WindowMessage) -> str:
    """A message's text as a harness reads it (user text blocks joined by newlines)."""
    if message["role"] == "user":
        return "\n".join(
            str(b.get("text", "")) for b in message["content"] if b.get("type") == "input_text"
        )
    if message["role"] == "assistant":
        return message["text"]
    return message["output"]


def window_tokens(messages: Sequence[WindowMessage], model: str) -> int:
    """The rendered window's size in tokens, counted as the fold counts (images at a flat
    attachment cost)."""
    total = 0
    for message in messages:
        total += _tokens(message_text(message), model)
        if message["role"] == "user":
            total += _ATTACHMENT_TOKENS * sum(
                1 for b in message["content"] if b.get("type") in _FILE_BLOCKS
            )
        elif message["role"] == "assistant":
            for call in message["tool_calls"]:
                total += _tokens(call["name"] + json.dumps(call["arguments"]), model)
    return total


def respawn_branch(*, file_matches: bool, renderable: bool, fits: bool) -> RespawnBranch:
    """Which start a Conversation's harness process gets (plan §3.3).

    ``reuse`` the harness's own session file when it is complete and its last texts match the
    record; else ``render`` the epoch when it is exactly renderable and fits (at most twice what
    a new session costs); else a ``new_epoch`` from today's assembled start.
    """
    if file_matches:
        return "reuse"
    if renderable and fits:
        return "render"
    return "new_epoch"
