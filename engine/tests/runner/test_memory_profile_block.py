"""Tests for ``_apply_memory_profile_to_body`` (per-turn Memory Profile delivery, S6).

Pure composition: given this turn's outgoing harness body and the rendered
(delimiter-wrapped) Memory Profile block, prepend it to the content the
engine sees, without mutating the input. The caller (``_run_turn_bg_setup_
and_stream``) only reaches this for ``superside-chat`` sessions and never
persists the result — covered by inspection (the persisted user item is
built from ``msg_body``, a separate dict the mutation never touches) and by
these shape tests, which mirror ``_prefix_content_with_tail``'s existing
coverage pattern for the analogous post-compaction tail.
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import _apply_memory_profile_to_body

_BLOCK = (
    "[Standing memory about the user — provided by the system, not a message "
    "from the user]\n\nPreferences:\n- Prefers figures in CAD\n\n"
    "[End of standing memory]"
)


def test_no_block_leaves_the_body_untouched() -> None:
    body = {"content": "hello"}
    assert _apply_memory_profile_to_body(body, None) is body


def test_empty_block_leaves_the_body_untouched() -> None:
    body = {"content": "hello"}
    assert _apply_memory_profile_to_body(body, "") is body


def test_prepends_to_a_plain_string_content() -> None:
    body = {"content": "hello"}
    result = _apply_memory_profile_to_body(body, _BLOCK)
    assert result["content"] == f"{_BLOCK}\n\nhello"
    # The input dict itself is never mutated.
    assert body["content"] == "hello"


def test_prepends_to_a_flat_content_blocks_list() -> None:
    body = {"content": [{"type": "input_text", "text": "hello"}]}
    result = _apply_memory_profile_to_body(body, _BLOCK)
    assert result["content"][0]["text"] == f"{_BLOCK}\n\nhello"
    assert body["content"][0]["text"] == "hello"


def test_prepends_to_the_latest_user_message_in_a_history_list() -> None:
    body = {
        "content": [
            {"type": "message", "role": "user", "content": [{"type": "text", "text": "first"}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "reply"}],
            },
            {"type": "message", "role": "user", "content": [{"type": "text", "text": "second"}]},
        ]
    }
    result = _apply_memory_profile_to_body(body, _BLOCK)
    messages = result["content"]
    assert messages[0]["content"][0]["text"] == "first"  # earlier turns untouched
    assert messages[1]["content"][0]["text"] == "reply"
    assert messages[2]["content"][0]["text"] == f"{_BLOCK}\n\nsecond"


def test_preserves_other_body_fields() -> None:
    body = {"content": "hello", "model": "test-model", "instructions": "be nice"}
    result = _apply_memory_profile_to_body(body, _BLOCK)
    assert result["model"] == "test-model"
    assert result["instructions"] == "be nice"


def test_prepends_to_latest_user_message_in_a_history_with_tool_items() -> None:
    """With tool items in history the tail still lands on the last user turn."""
    history = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"}]},
        {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "r"}],
        },
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "time?"}]},
    ]
    result = _apply_memory_profile_to_body({"content": history}, _BLOCK)["content"]
    assert len(result) == len(history)
    assert result[-1]["content"][0]["text"] == f"{_BLOCK}\n\ntime?"
    assert result[0] == history[0]


# ── the turn prefix: gap line and the unchanged-block lines ──────────────


class _ProfileServer:
    """Serves one Memory Profile, which a test can change."""

    def __init__(self, profile: str | None) -> None:
        self.profile = profile

    async def get(self, url: str, **_kwargs: object) -> object:
        profile = self.profile

        class _Response:
            status_code = 200

            def json(self) -> dict[str, object]:
                return {"profile": profile} if url.endswith("/memory/profile") else {}

        return _Response()


def test_idle_gap_line_is_one_plain_sentence() -> None:
    from omnigent.superchat.prompt_prefix import idle_gap_line

    assert idle_gap_line(3 * 86_400 + 5) == "It has been 3 days since the last message."
    assert idle_gap_line(13 * 3600) == "It has been 13 hours since the last message."
    assert idle_gap_line(3600) == "It has been 1 hour since the last message."
    assert idle_gap_line(5) == "It has been less than an hour since the last message."
    assert "\n" not in idle_gap_line(86_400 * 40)


async def test_turn_prefix_sends_a_block_in_full_only_when_it_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.superchat import prompt_prefix

    projects = "[Projects]\n- taxes"
    monkeypatch.setattr(prompt_prefix, "_safe_projects_block", lambda _w: projects)
    server = _ProfileServer(_BLOCK)

    async def build(last_sent: dict[str, str] | None) -> tuple[list[str], dict[str, str]]:
        sent_now: dict[str, str] = {}
        blocks = await prompt_prefix.turn_prefix_blocks(
            server,
            "conv",
            None,
            last_sent=last_sent,
            sent_now=sent_now,  # type: ignore[arg-type]
        )
        return blocks, sent_now

    first, sent = await build(None)
    assert _BLOCK in first and projects in first

    again, sent_again = await build(sent)
    assert _BLOCK not in again and projects not in again
    assert prompt_prefix.PROFILE_UNCHANGED_LINE in again
    assert prompt_prefix.PROJECTS_UNCHANGED_LINE in again
    assert sent_again == sent  # still held by the same client

    server.profile = _BLOCK.replace("CAD", "EUR")
    changed, _ = await build(sent)
    assert server.profile in changed and projects not in changed
    assert prompt_prefix.PROJECTS_UNCHANGED_LINE in changed

    # The local-time line is never replaced; a gap line sits next to it.
    gap = await prompt_prefix.turn_prefix_blocks(
        server,
        "conv",
        None,
        idle_gap_seconds=86_400 * 3,
        last_sent=sent,  # type: ignore[arg-type]
    )
    assert gap[-1].startswith("[Current local time")
    assert gap[-2] == "It has been 3 days since the last message."


async def test_a_profile_that_goes_away_is_not_remembered_as_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.superchat import prompt_prefix

    monkeypatch.setattr(prompt_prefix, "_safe_projects_block", lambda _w: None)
    server = _ProfileServer(None)
    sent_now: dict[str, str] = {}

    blocks = await prompt_prefix.turn_prefix_blocks(
        server,
        "conv",
        None,
        last_sent={"profile": "stale"},
        sent_now=sent_now,  # type: ignore[arg-type]
    )

    assert sent_now == {}
    assert len(blocks) == 1  # local time only


async def test_turn_prefix_fetches_its_blocks_together_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0026: the prefix costs its slowest read, not the sum; the order stays documented."""
    import asyncio
    import time

    from omnigent.superchat import prompt_prefix

    delay = 0.3

    async def profile(_server: object, _conv: str) -> str:
        await asyncio.sleep(delay)
        return "PROFILE"

    def projects(_workspace: object) -> str:
        time.sleep(delay)
        return "PROJECTS"

    async def features(_server: object, _conv: str) -> list[str]:
        await asyncio.sleep(delay)
        return ["EDIT NOTE", "ARTIFACT HINT"]

    async def local_time(_server: object) -> str:
        await asyncio.sleep(delay)
        return "LOCAL TIME"

    monkeypatch.setattr(prompt_prefix, "fetch_memory_profile", profile)
    monkeypatch.setattr(prompt_prefix, "_safe_projects_block", projects)
    monkeypatch.setattr(prompt_prefix, "_feature_prefix_blocks", features)
    monkeypatch.setattr(prompt_prefix, "fetch_local_time_line", local_time)

    started = time.monotonic()
    blocks = await prompt_prefix.turn_prefix_blocks(None, "conv", None, idle_gap_seconds=5.0)
    elapsed = time.monotonic() - started

    assert blocks == [
        "PROFILE",
        "PROJECTS",
        "EDIT NOTE",
        "ARTIFACT HINT",
        "It has been less than an hour since the last message.",
        "LOCAL TIME",
    ]
    assert elapsed < 3 * delay


async def test_feature_blocks_keep_feature_order_and_a_failing_one_drops_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from types import SimpleNamespace

    from omnigent.superchat import features, prompt_prefix

    async def slow(_server: object, _conv: str) -> list[str]:
        await asyncio.sleep(0.05)
        return ["first"]

    async def broken(_server: object, _conv: str) -> list[str]:
        raise RuntimeError("down")

    async def fast(_server: object, _conv: str) -> list[str]:
        return ["third"]

    monkeypatch.setattr(
        features,
        "FEATURES",
        [
            SimpleNamespace(name="slow", turn_prefix=slow),
            SimpleNamespace(name="none", turn_prefix=None),
            SimpleNamespace(name="broken", turn_prefix=broken),
            SimpleNamespace(name="fast", turn_prefix=fast),
        ],
    )

    assert await prompt_prefix._feature_prefix_blocks(None, "conv") == ["first", "third"]
