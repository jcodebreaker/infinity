"""The window renderer: the record as the model saw it (golden cases), and the respawn rule."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.context.window import (
    INTERRUPTED_RESULT,
    TURN_START,
    exactly_renderable,
    pending_message,
    render_window,
    respawn_branch,
)

_IMAGE = "data:image/png;base64,iVBORw0KGgo="


def _user(item_id: str, text: str, resp: str = "", **fields: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "user",
        "response_id": resp or f"user_{item_id}",
        "content": [{"type": "input_text", "text": text}],
        **fields,
    }


def _said(item_id: str, text: str, resp: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "response_id": resp,
        "content": [{"type": "output_text", "text": text}],
    }


def _call(call_id: str, resp: str) -> dict[str, Any]:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "response_id": resp,
        "call_id": call_id,
        "name": "shell",
        "arguments": '{"cmd": "ls"}',
    }


def _result(call_id: str, resp: str, output: str = "ok") -> dict[str, Any]:
    return {
        "id": f"out_{call_id}",
        "type": "function_call_output",
        "response_id": resp,
        "call_id": call_id,
        "output": output,
    }


def _texts(window: list[Any]) -> list[str]:
    """Each message as ``role: text`` (a tool result as ``tool <id>: output``)."""
    out: list[str] = []
    for m in window:
        if m["role"] == "user":
            out.append("user: " + "|".join(str(b.get("text")) for b in m["content"]))
        elif m["role"] == "assistant":
            calls = ",".join(c["id"] for c in m["tool_calls"])
            out.append(f"assistant: {m['text']}" + (f" [{calls}]" if calls else ""))
        else:
            out.append(f"tool {m['call_id']}: {m['output']}")
    return out


def test_steers_go_after_the_whole_batch_and_at_turn_start() -> None:
    # Both steers were stored when the person sent them, before the outputs they followed.
    items = [
        _user("u1", "find it", context_prefix=["[Local time: 9:00]"]),
        _user("s0", "quickly", delivered_after_call_id=TURN_START),
        _said("a1", "looking", "r1"),
        _user("s1", "also the logs", delivered_after_call_id="c1"),
        _call("c1", "r1"),
        _call("c2", "r1"),
        _result("c1", "r1"),
        _result("c2", "r1"),
        _said("a2", "found", "r1"),
    ]

    assert _texts(render_window("BLOCK", items)) == [
        "user: BLOCK",
        "user: [Local time: 9:00]\n\nfind it",
        "user: quickly",
        "assistant: looking [c1,c2]",
        "tool c1: ok",
        "tool c2: ok",
        "user: also the logs",
        "assistant: found",
    ]
    assert exactly_renderable("BLOCK", items)


def test_a_message_sent_while_a_turn_ran_follows_that_turn() -> None:
    items = [
        _user("u1", "first", context_prefix=[]),
        _said("a1", "working", "r1"),
        _user("u2", "second", context_prefix=[]),
        _call("c1", "r1"),
        _result("c1", "r1"),
        _said("a2", "done one", "r1"),
        _said("a3", "done two", "r2"),
    ]

    assert _texts(render_window(None, items)) == [
        "user: first",
        "assistant: working [c1]",
        "tool c1: ok",
        "assistant: done one",
        "user: second",
        "assistant: done two",
    ]
    assert exactly_renderable("", items)


def test_a_helper_notice_is_a_user_message_as_stored() -> None:
    notice = "[System: Helper research finished]\nThree sources agree."
    items = [
        _user("u1", "research it", context_prefix=[]),
        _said("a1", "started a Helper", "r1"),
        _user("n1", notice, is_system_notice=True, context_prefix=["[Local time: 9:05]"]),
        _said("a2", "they agree", "r2"),
    ]

    assert _texts(render_window(None, items))[2:] == [
        f"user: [Local time: 9:05]\n\n{notice}",
        "assistant: they agree",
    ]


def test_an_interrupted_call_gets_a_result_and_the_partial_text_stays() -> None:
    items = [
        _user("u1", "deploy", context_prefix=[]),
        _said("a1", "deploying", "r1"),
        _call("c1", "r1"),
        _user("m1", "[System: interrupted]"),
        _user("u2", "stop that", context_prefix=[]),
        _said("a2", "stopped", "r2"),
    ]

    assert _texts(render_window(None, items)) == [
        "user: deploy",
        "assistant: deploying [c1]",
        f"tool c1: {INTERRUPTED_RESULT}",
        "user: [System: interrupted]",
        "user: stop that",
        "assistant: stopped",
    ]


def test_images_stay_inline_for_the_last_two_rounds_only() -> None:
    def with_image(item_id: str, name: str) -> dict[str, Any]:
        item = _user(item_id, f"look at {name}", context_prefix=[])
        item["content"].append({"type": "input_image", "image_url": _IMAGE, "filename": name})
        return item

    items = [
        with_image("u1", "old.png"),
        _said("a1", "seen", "r1"),
        with_image("u2", "mid.png"),
        _said("a2", "seen", "r2"),
        with_image("u3", "new.png"),
        _said("a3", "seen", "r3"),
    ]

    window = render_window(None, items)
    assert window[0]["content"][1] == {
        "type": "input_text",
        "text": "[image: old.png, in the Library]",
    }
    assert [b["type"] for b in window[2]["content"]] == ["input_text", "input_image"]
    assert [b["type"] for b in window[4]["content"]] == ["input_text", "input_image"]


def test_reasoning_is_left_out() -> None:
    items = [
        _user("u1", "why", context_prefix=[]),
        {"id": "rs1", "type": "reasoning", "response_id": "r1", "summary": [], "content": []},
        _said("a1", "because", "r1"),
    ]

    assert _texts(render_window(None, items)) == ["user: why", "assistant: because"]


def test_exactly_renderable_needs_the_block_and_every_turns_prefix() -> None:
    written = [_user("u1", "hi", context_prefix=[]), _said("a1", "hello", "r1")]
    unknown = [_user("u1", "hi"), _said("a1", "hello", "r1")]
    # The message the next turn answers has no answer yet: it is not part of the epoch's turns.
    waiting = [*written, _user("u2", "and?")]

    assert exactly_renderable("BLOCK", written)
    assert exactly_renderable("", written)
    assert not exactly_renderable(None, written)
    assert not exactly_renderable("BLOCK", unknown)
    assert exactly_renderable("BLOCK", waiting)
    assert pending_message(waiting) == "u2"
    assert pending_message(written) is None


@pytest.mark.parametrize(
    ("file_matches", "renderable", "fits", "branch"),
    [
        (True, False, False, "reuse"),
        (True, True, True, "reuse"),
        (False, True, True, "render"),
        (False, True, False, "new_epoch"),
        (False, False, True, "new_epoch"),
    ],
)
def test_respawn_branch(file_matches: bool, renderable: bool, fits: bool, branch: str) -> None:
    assert respawn_branch(file_matches=file_matches, renderable=renderable, fits=fits) == branch
