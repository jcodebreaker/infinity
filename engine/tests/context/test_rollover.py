"""Unit tests for omnigent.context.rollover."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnigent.context.rollover import (
    CHECKPOINT_HEADER,
    DEFAULT_ROLLOVER_THRESHOLD_TOKENS,
    HANDOFF_THRESHOLD_TOKENS,
    build_rollover_item,
    fold_input,
    is_checkpoint_text,
    person_facing_summary,
    resolve_keep_tokens,
    resolve_rollover_threshold,
    select_recent,
    state_file_summarizer_instruction,
)
from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.runner.memory.files import state_text_of
from omnigent.runtime.compaction import count_text_tokens, count_tokens
from omnigent.server.routes._sessions.common import (
    _LAST_CONTEXT_WINDOW_LABEL_KEY,
)

# Generous enough that no test below hits it unless it's specifically
# exercising the token budget.
_HUGE_TOKENS = 1_000_000

# A reply the fold accepts: tagged entries plus a journal.
_STATE = "- [t-a · 2026-10-12] Alpha"
_REPLY = f"{_STATE}\n<journal>\n2026-10-12 · did it\n</journal>"


def _msg(item_id: str, role: str, text: str, response_id: str | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "response_id": response_id or f"resp_{item_id}",
        "created_at": 1,
        "role": role,
        "content": [
            {"type": "input_text" if role == "user" else "output_text", "text": text},
        ],
    }


def _tool_pair(call_id: str, response_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"fc_{call_id}",
            "type": "function_call",
            "status": "completed",
            "response_id": response_id,
            "created_at": 1,
            "call_id": call_id,
            "name": "some_tool",
            "arguments": "{}",
        },
        {
            "id": f"fo_{call_id}",
            "type": "function_call_output",
            "status": "completed",
            "response_id": response_id,
            "created_at": 1,
            "call_id": call_id,
            "output": "ok",
        },
    ]


def _turn(n: int, *, with_tool: bool = False) -> list[dict[str, Any]]:
    """One whole turn: a user message, optionally a tool round-trip, then
    an assistant reply — the unit select_recent now walks in whole."""
    items = [_msg(f"u{n}", "user", f"question {n}", response_id=f"r{n}")]
    if with_tool:
        items += _tool_pair(f"c{n}", f"r{n}")
    items.append(_msg(f"a{n}", "assistant", f"answer {n}", response_id=f"r{n}"))
    return items


class _ReturnsTextClient:
    """LLM client stub returning a fixed summary, matching the real Response shape."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.call_count = 0
        self.seen_messages: list[dict[str, Any]] | None = None
        self.seen_instructions: str | None = None

    class _Responses:
        def __init__(self, outer: _ReturnsTextClient) -> None:
            self._outer = outer

        async def create(self, **kwargs: Any) -> Response:
            self._outer.call_count += 1
            self._outer.seen_messages = kwargs.get("input")
            self._outer.seen_instructions = kwargs.get("instructions")
            return Response(
                output=[MessageOutput(content=[OutputText(text=self._outer._text)])],
                model="test-model",
            )

    @property
    def responses(self) -> _ReturnsTextClient._Responses:
        return self._Responses(self)


# ── select_recent: whole-turn selection ─────────────────────────────────


def test_select_recent_always_keeps_the_last_turn() -> None:
    """Rollover runs right after a turn finishes; that turn stays verbatim
    even when it alone is over budget."""
    items = _turn(1) + _turn(2)
    result = select_recent(items, keep_tokens=1, model="gpt-4o")
    assert [i["id"] for i in result] == ["u2", "a2"]


def test_select_recent_keeps_tool_items_riding_within_their_turn() -> None:
    items = _turn(1) + _turn(2, with_tool=True)
    result = select_recent(items, keep_tokens=1, model="gpt-4o")
    assert [i["id"] for i in result] == ["u2", "fc_c2", "fo_c2", "a2"]


def test_select_recent_adds_earlier_whole_turns_within_budget() -> None:
    items = _turn(1) + _turn(2) + _turn(3)
    # Counted as rendered ("role: text" lines, not the raw item JSON), with the 15% margin.
    two_turns = sum(
        int(count_text_tokens(f"user: question {n}\nassistant: answer {n}", "gpt-4o") * 1.15)
        for n in (2, 3)
    )
    result = select_recent(items, keep_tokens=two_turns, model="gpt-4o")
    assert [i["id"] for i in result] == ["u2", "a2", "u3", "a3"]


def test_select_recent_tail_always_starts_at_a_user_message() -> None:
    items = _turn(1) + _turn(2, with_tool=True) + _turn(3)
    for budget in (1, 50, 200, _HUGE_TOKENS):
        result = select_recent(items, keep_tokens=budget, model="gpt-4o")
        assert result[0]["role"] == "user", f"budget={budget} started on {result[0]!r}"


def test_select_recent_empty_items() -> None:
    assert select_recent([], keep_tokens=_HUGE_TOKENS, model="gpt-4o") == []


def test_select_recent_no_user_message_is_empty_tail() -> None:
    # Malformed/partial record with no turn to anchor a tail on.
    items = [_msg("a1", "assistant", "orphan reply")]
    assert select_recent(items, keep_tokens=_HUGE_TOKENS, model="gpt-4o") == []


# ── threshold / keep-budget resolution ──────────────────────────────────


def test_threshold_default_when_no_label_or_window() -> None:
    assert resolve_rollover_threshold(None) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS
    assert resolve_rollover_threshold({}) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_threshold_falls_back_to_60pct_of_context_window() -> None:
    labels = {_LAST_CONTEXT_WINDOW_LABEL_KEY: "190000"}
    assert resolve_rollover_threshold(labels) == 114_000


def test_threshold_default_caps_at_the_handoff_for_a_huge_window() -> None:
    # 60% of 1M would be 600,000; the default is capped at the 120,000 handoff.
    labels = {_LAST_CONTEXT_WINDOW_LABEL_KEY: "1000000"}
    assert resolve_rollover_threshold(labels) == HANDOFF_THRESHOLD_TOKENS


def test_threshold_explicit_label_is_not_capped() -> None:
    # The cap applies only to the derived default, never to an explicit label.
    labels = {
        "omnigent.context.rollover_at_tokens": "250000",
        _LAST_CONTEXT_WINDOW_LABEL_KEY: "1000000",
    }
    assert resolve_rollover_threshold(labels) == 250_000


def test_threshold_uses_model_window_when_no_window_label() -> None:
    assert resolve_rollover_threshold(None, model_window=190_000) == 114_000


def test_threshold_window_label_wins_over_model_window() -> None:
    # 60% of the labeled 100,000 window: proof the much larger model_window is ignored.
    labels = {_LAST_CONTEXT_WINDOW_LABEL_KEY: "100000"}
    assert resolve_rollover_threshold(labels, model_window=1_000_000) == 60_000


def test_threshold_ignores_non_positive_model_window() -> None:
    assert resolve_rollover_threshold(None, model_window=0) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_threshold_explicit_label_wins_over_window() -> None:
    labels = {
        "omnigent.context.rollover_at_tokens": "150000",
        _LAST_CONTEXT_WINDOW_LABEL_KEY: "400000",
    }
    assert resolve_rollover_threshold(labels) == 150_000


def test_threshold_has_no_floor() -> None:
    assert resolve_rollover_threshold({"omnigent.context.rollover_at_tokens": "12345"}) == 12_345
    # A small window gives 60% of it: a new session assembles far below that.
    assert resolve_rollover_threshold({_LAST_CONTEXT_WINDOW_LABEL_KEY: "100000"}) == 60_000
    labels = {
        "omnigent.context.rollover_at_tokens": "5000",
        _LAST_CONTEXT_WINDOW_LABEL_KEY: "100000",
    }
    assert resolve_rollover_threshold(labels) == 5_000


def test_threshold_ignores_invalid_label() -> None:
    labels = {"omnigent.context.rollover_at_tokens": "not-a-number"}
    assert resolve_rollover_threshold(labels) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_resolve_keep_tokens_default_and_override() -> None:
    assert resolve_keep_tokens(None) == 8_000
    assert resolve_keep_tokens({"omnigent.context.rollover_keep_tokens": "5000"}) == 5000
    assert resolve_keep_tokens({"omnigent.context.rollover_keep_tokens": "-1"}) == 8_000


# ── build_rollover_item + resume-rebuilder acceptance ────────────────────


@pytest.mark.asyncio
async def test_build_rollover_item_shape() -> None:
    items = _turn(1) + _turn(2) + _turn(3)
    client = _ReturnsTextClient(_REPLY)

    data = await build_rollover_item(
        items,
        previous_summary=None,
        model="gpt-4o",
        llm_client=client,
    )

    assert data.summary == f"{CHECKPOINT_HEADER}\n\n{_STATE}"
    assert data.last_item_id == "a3"  # the window's last item
    assert data.token_count == count_tokens(data.compacted_messages, "gpt-4o")
    # Only the summary pair: no kept tail.
    assert data.compacted_messages is not None
    assert [m.get("role") for m in data.compacted_messages] == ["user", "assistant"]
    assert data.compacted_messages[1]["content"][0]["text"] == _STATE


@pytest.mark.asyncio
async def test_build_rollover_item_passes_state_file_instruction() -> None:
    items = _turn(1)
    client = _ReturnsTextClient(_REPLY)

    await build_rollover_item(
        items,
        previous_summary=None,
        model="gpt-4o",
        llm_client=client,
    )

    assert client.seen_instructions is not None
    assert "state file" in client.seen_instructions
    assert "Context checkpoint" in client.seen_instructions


@pytest.mark.asyncio
async def test_build_rollover_item_feeds_previous_summary_for_progressive_summarization() -> None:
    items = _turn(1)
    client = _ReturnsTextClient(_REPLY)

    await build_rollover_item(
        items,
        previous_summary=f"{CHECKPOINT_HEADER}\n\nOLD SUMMARY of earlier turns",
        model="gpt-4o",
        llm_client=client,
    )

    assert client.seen_messages is not None
    first_text = client.seen_messages[0]["content"][0]["text"]
    assert "automatically generated summary" in first_text


@pytest.mark.asyncio
async def test_rollover_marks_stale_open_entries_where_the_model_and_state_md_read() -> None:
    today = datetime.now(UTC).date()
    old, recent = today - timedelta(days=30), today - timedelta(days=29)
    previous = f"{CHECKPOINT_HEADER}\n\n- [o-dana · {old} · stale?] waiting on Dana's reply"
    # The summarizer keeps the mark it was shown and adds a 29-day-old and a closed entry.
    client = _ReturnsTextClient(
        f"- [o-dana · {old} · stale?] waiting on Dana's reply\n"
        f"- [o-quotes · {recent}] three quotes requested\n"
        f"- [t-venue · {old}] closed: the venue is booked\n"
        "<journal>\n2026-10-12 · checked\n</journal>"
    )

    data = await build_rollover_item(
        _turn(1),
        previous_summary=previous,
        model="gpt-4o",
        llm_client=client,
    )

    state = (
        f"- [o-dana · {old} · stale?] waiting on Dana's reply\n"
        f"- [o-quotes · {recent}] three quotes requested\n"
        f"- [t-venue · {old}] closed: the venue is booked"
    )
    assert data.repairs == 0
    assert data.summary == f"{CHECKPOINT_HEADER}\n\n{state}"
    assert data.compacted_messages is not None
    assert data.compacted_messages[1]["content"][0]["text"] == state
    assert state_text_of(data.summary) == state  # what STATE.md is written from


@pytest.mark.asyncio
async def test_build_rollover_item_requires_nonempty_items() -> None:
    with pytest.raises(ValueError):
        await build_rollover_item(
            [],
            previous_summary=None,
            model="gpt-4o",
            llm_client=_ReturnsTextClient(_REPLY),
        )


def test_state_file_summarizer_instruction_is_fixed_text_without_a_date() -> None:
    import re

    instruction = state_file_summarizer_instruction()
    assert "## Context checkpoint —" in instruction
    assert "'Now:' line" in instruction
    assert not re.search(r"\d{4}-\d{2}-\d{2}", instruction)
    assert instruction == state_file_summarizer_instruction()


@pytest.mark.asyncio
async def test_checkpoint_marker_differs_from_the_summarizer_request() -> None:
    """The CLI must see a marker that's plainly not from the user, while the
    summarizer keeps the request wording it expects for a previous summary."""
    from omnigent.context.rollover import _SUMMARY_REQUEST_TEXT

    client = _ReturnsTextClient(_REPLY)
    data = await build_rollover_item(
        _turn(1) + _turn(2),
        previous_summary="OLD SUMMARY",
        model="gpt-4o",
        llm_client=client,
    )
    marker = data.compacted_messages[0]["content"][0]["text"]
    assert marker == CHECKPOINT_HEADER
    assert "not a message from the user" in marker
    assert _SUMMARY_REQUEST_TEXT != CHECKPOINT_HEADER


@pytest.mark.asyncio
async def test_summarizer_gets_one_transcript_message_not_live_turns() -> None:
    """Live chat turns let a weak summarizer continue the chat; a single quoted
    transcript (with the previous summary on top) can only be summarized."""
    client = _ReturnsTextClient(_REPLY)
    await build_rollover_item(
        _turn(1) + _turn(2),
        previous_summary="OLD SUMMARY",
        model="gpt-4o",
        llm_client=client,
    )
    assert client.seen_messages is not None
    user_texts = [
        block["text"]
        for message in client.seen_messages
        if message.get("role") == "user"
        for block in message["content"]
        if isinstance(block, dict) and "text" in block
    ]
    transcript = user_texts[0]
    assert "OLD SUMMARY" in transcript
    assert "<turns>" in transcript
    assert not any(m.get("role") == "assistant" for m in client.seen_messages)
    assert "automatically generated summary" in client.seen_instructions + transcript


@pytest.mark.asyncio
async def test_previous_header_is_not_fed_back_so_it_never_nests() -> None:
    client = _ReturnsTextClient(_REPLY)
    data = await build_rollover_item(
        _turn(1),
        previous_summary=f"{CHECKPOINT_HEADER}\n\nOLD SUMMARY",
        model="gpt-4o",
        llm_client=client,
    )
    transcript = client.seen_messages[0]["content"][0]["text"]
    assert "OLD SUMMARY" in transcript
    assert CHECKPOINT_HEADER not in transcript
    assert data.summary.count(CHECKPOINT_HEADER) == 1


# ---------------------------------------------------------------------------
# build_post_compaction_tail
# ---------------------------------------------------------------------------


def _rollover_labels(extra: dict[str, str] | None = None) -> dict[str, str]:
    from omnigent.context.labels import CONTEXT_MODE_LABEL, ROLLOVER_MODE_VALUE

    return {CONTEXT_MODE_LABEL: ROLLOVER_MODE_VALUE, **(extra or {})}


def _compaction_item(item_id: str = "comp_1") -> dict[str, Any]:
    return {"id": item_id, "type": "compaction", "summary": "…", "last_item_id": "u1"}


def test_post_compaction_tail_none_without_rollover_label() -> None:
    from omnigent.context.rollover import build_post_compaction_tail

    items = [*_turn(1), _compaction_item()]
    assert build_post_compaction_tail(items, {}, model="gpt-4o") is None
    assert build_post_compaction_tail(items, None, model="gpt-4o") is None


def test_post_compaction_tail_none_without_a_compaction_item() -> None:
    from omnigent.context.rollover import build_post_compaction_tail

    assert build_post_compaction_tail(_turn(1), _rollover_labels(), model="gpt-4o") is None


def test_post_compaction_tail_none_when_compaction_is_the_first_item() -> None:
    from omnigent.context.rollover import build_post_compaction_tail

    items = [_compaction_item(), *_turn(1)]
    assert build_post_compaction_tail(items, _rollover_labels(), model="gpt-4o") is None


def test_post_compaction_tail_renders_markers_and_whole_turns() -> None:
    from omnigent.context.rollover import build_post_compaction_tail

    items = [*_turn(1, with_tool=True), _compaction_item()]
    tail = build_post_compaction_tail(items, _rollover_labels(), model="gpt-4o")
    assert tail is not None
    lines = tail.splitlines()
    assert lines[0] == (
        "[Recent conversation before the context was compacted — verbatim, "
        "provided by the system, not a new message from the user]"
    )
    assert lines[-1] == "[End of recent conversation]"
    assert "User: question 1" in tail
    assert "Assistant: answer 1" in tail
    assert "Tool call some_tool: {}" in tail
    assert "Tool result: ok" in tail


def test_post_compaction_tail_selection_stays_within_keep_tokens_budget() -> None:
    """Only the trailing whole turn survives a tight budget."""
    from omnigent.context.labels import ROLLOVER_KEEP_TOKENS_LABEL
    from omnigent.context.rollover import build_post_compaction_tail

    items = [*_turn(1), *_turn(2), _compaction_item()]
    tail = build_post_compaction_tail(
        items, _rollover_labels({ROLLOVER_KEEP_TOKENS_LABEL: "1"}), model="gpt-4o"
    )
    assert tail is not None
    assert "question 1" not in tail
    assert "question 2" in tail
    assert "answer 2" in tail


def test_post_compaction_tail_caps_oversized_tool_output() -> None:
    from omnigent.context.rollover import _KEPT_OUTPUT_MAX_CHARS, build_post_compaction_tail

    items = _turn(1, with_tool=True)
    for item in items:
        if item.get("type") == "function_call_output":
            item["output"] = "x" * (_KEPT_OUTPUT_MAX_CHARS * 5)
    items.append(_compaction_item())
    tail = build_post_compaction_tail(items, _rollover_labels(), model="gpt-4o")
    assert tail is not None
    assert "x" * (_KEPT_OUTPUT_MAX_CHARS * 5) not in tail
    assert "output truncated at rollover" in tail


def test_post_compaction_tail_excludes_reasoning_and_lifecycle_items() -> None:
    from omnigent.context.rollover import build_post_compaction_tail

    items = [
        {"id": "r1", "type": "reasoning", "summary": []},
        *_turn(1),
        {"id": "err1", "type": "error", "message": "boom"},
        _compaction_item(),
    ]
    tail = build_post_compaction_tail(items, _rollover_labels(), model="gpt-4o")
    assert tail is not None
    assert "boom" not in tail
    assert "question 1" in tail


def test_post_compaction_tail_ignores_items_after_the_latest_compaction() -> None:
    """Only the LATEST compaction's boundary matters; a turn that happened
    AFTER it (not yet compacted) is never part of the tail."""
    from omnigent.context.rollover import build_post_compaction_tail

    items = [
        *_turn(1),
        _compaction_item("comp_1"),
        *_turn(2),
        _compaction_item("comp_2"),
        *_turn(3),
    ]
    tail = build_post_compaction_tail(items, _rollover_labels(), model="gpt-4o")
    assert tail is not None
    assert "question 2" in tail
    assert "question 3" not in tail


# ---------------------------------------------------------------------------
# consume_post_compaction_tail / prefix_latest_user_item
# ---------------------------------------------------------------------------


def test_consume_post_compaction_tail_fires_once_per_compaction() -> None:
    from omnigent.context.rollover import consume_post_compaction_tail

    items = [*_turn(1), _compaction_item()]
    first = consume_post_compaction_tail(
        items, _rollover_labels(), session_id="consume-once", model="gpt-4o"
    )
    assert first is not None
    assert "question 1" in first

    second = consume_post_compaction_tail(
        items, _rollover_labels(), session_id="consume-once", model="gpt-4o"
    )
    assert second is None


def test_consume_post_compaction_tail_re_arms_on_a_later_compaction() -> None:
    from omnigent.context.rollover import consume_post_compaction_tail

    session_id = "consume-rearm"
    first_items = [*_turn(1), _compaction_item("comp_a")]
    assert (
        consume_post_compaction_tail(
            first_items, _rollover_labels(), session_id=session_id, model="gpt-4o"
        )
        is not None
    )

    later_items = [*first_items, *_turn(2), _compaction_item("comp_b")]
    second = consume_post_compaction_tail(
        later_items, _rollover_labels(), session_id=session_id, model="gpt-4o"
    )
    assert second is not None
    assert "question 2" in second


def test_consume_post_compaction_tail_scoped_per_session() -> None:
    """Two sessions sharing the same compaction id each get the tail once."""
    from omnigent.context.rollover import consume_post_compaction_tail

    items = [*_turn(1), _compaction_item("shared_comp")]
    for session_id in ("consume-session-a", "consume-session-b"):
        tail = consume_post_compaction_tail(
            items, _rollover_labels(), session_id=session_id, model="gpt-4o"
        )
        assert tail is not None


def test_consume_post_compaction_tail_none_without_a_compaction() -> None:
    from omnigent.context.rollover import consume_post_compaction_tail

    assert (
        consume_post_compaction_tail(
            _turn(1), _rollover_labels(), session_id="consume-no-compaction", model="gpt-4o"
        )
        is None
    )


def test_prefix_latest_user_item_adds_to_existing_text_block() -> None:
    from omnigent.context.rollover import prefix_latest_user_item

    items = [*_turn(1), _msg("u2", "user", "pending question")]
    updated = prefix_latest_user_item(items, "TAIL")
    assert updated[-1]["content"][0]["text"] == "TAIL\n\npending question"
    # Earlier items and the original list are untouched.
    assert items[-1]["content"][0]["text"] == "pending question"
    assert updated[0] is items[0]


def test_prefix_latest_user_item_handles_string_content() -> None:
    from omnigent.context.rollover import prefix_latest_user_item

    items = [{"type": "message", "role": "user", "content": "pending question"}]
    updated = prefix_latest_user_item(items, "TAIL")
    assert updated[0]["content"] == "TAIL\n\npending question"


def test_prefix_latest_user_item_inserts_block_when_text_blocks_absent() -> None:
    from omnigent.context.rollover import prefix_latest_user_item

    items = [{"type": "message", "role": "user", "content": [{"type": "input_image"}]}]
    updated = prefix_latest_user_item(items, "TAIL")
    assert updated[0]["content"][0] == {"type": "input_text", "text": "TAIL"}


def test_prefix_latest_user_item_no_user_message_is_a_no_op() -> None:
    from omnigent.context.rollover import prefix_latest_user_item

    items = [_msg("a1", "assistant", "hello")]
    assert prefix_latest_user_item(items, "TAIL") == items


def test_list_related_chats_does_not_match_two_unowned_sessions(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sessions with no owner grant on record must not read as
    related to each other — ``None == None`` is not "same owner" outside
    single-user mode, where every session's owner reads back ``None``."""
    from omnigent.context.labels import SCHEDULED_HELPER_LABEL_KEY
    from omnigent.context.rollover import list_related_chats
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    conv_store = SqlAlchemyConversationStore(db_uri)

    unowned_parent = conv_store.create_conversation()
    unowned_child = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=unowned_parent.id,
        labels={SCHEDULED_HELPER_LABEL_KEY: "t1"},
    )

    assert list_related_chats(conv_store, unowned_child.id) == []


def test_list_related_chats_single_user_mode_still_matches_unowned_sessions(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a single-user server (no permission store ever writes grants),
    every session's owner reads back ``None`` — the one case two ``None``s
    are the same (one) user, so related chats keep working."""
    from omnigent.context.labels import SCHEDULED_HELPER_LABEL_KEY
    from omnigent.context.rollover import list_related_chats
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    conv_store = SqlAlchemyConversationStore(db_uri)

    parent = conv_store.create_conversation()
    child = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        labels={SCHEDULED_HELPER_LABEL_KEY: "t1"},
    )

    related_ids = [chat["id"] for chat in list_related_chats(conv_store, child.id)]
    assert related_ids == [parent.id]


def test_list_related_chats_scheduled_helper_sees_parent_but_plain_subagent_does_not(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled Helper (Study, Check-in) may read its parent chat; an ordinary
    Sub-agent stays confined to its Brief."""
    from omnigent.context.labels import ADHOC_HELPER_LABEL_KEY, SCHEDULED_HELPER_LABEL_KEY
    from omnigent.context.rollover import list_related_chats
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent = conv_store.create_conversation()
    adhoc = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        title="analyst:Daily note",
        labels={ADHOC_HELPER_LABEL_KEY: "t2"},
    )
    assert [c["id"] for c in list_related_chats(conv_store, adhoc.id)] == [parent.id]
    scheduled = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        title="analyst:Study",
        labels={SCHEDULED_HELPER_LABEL_KEY: "t1"},
    )
    plain = conv_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title="analyst:other"
    )

    assert [c["id"] for c in list_related_chats(conv_store, scheduled.id)] == [parent.id]
    assert list_related_chats(conv_store, plain.id) == []


def test_summarizer_instruction_forbids_listing_tool_names() -> None:
    assert "Never list tool names" in state_file_summarizer_instruction()


def test_fold_input_stamps_time_and_zone_on_the_now_line() -> None:
    """A bare date is ambiguous across time zones; the input carries time and UTC."""
    text = fold_input(_turn(1), None, now="2026-10-12 10:00 UTC")["content"][0]["text"]

    assert "Now: 2026-10-12 10:00 UTC" in text


@pytest.mark.asyncio
async def test_build_rollover_item_rejects_an_empty_summary() -> None:
    """An empty/blank summarizer reply must raise so the caller keeps the previous checkpoint."""
    with pytest.raises(ValueError, match="no tagged entries or no journal"):
        await build_rollover_item(
            _turn(1) + _turn(2),
            previous_summary=None,
            model="gpt-4o",
            llm_client=_ReturnsTextClient("   "),
        )


def test_summarizer_instruction_forbids_recording_tool_errors_as_facts() -> None:
    text = state_file_summarizer_instruction()
    assert "Tool errors" in text
    assert "never claim earlier messages are inaccessible" in text


def test_person_facing_summary_drops_framing_title_and_tool_sections() -> None:
    """A checkpoint reads to the person as its body only, never as the model's framing."""
    raw = "\n".join(
        [
            CHECKPOINT_HEADER,
            "",
            "## Context checkpoint — 2026-10-04 10:00 UTC",
            "**Goal**: plan Q4",
            "",
            "## Technical capabilities confirmed",
            "- sys_session_send, sys_os_write",
            "",
            "## Current position / next step",
            "Draft the checklist.",
        ]
    )
    assert person_facing_summary(raw) == (
        "**Goal**: plan Q4\n\n## Current position / next step\nDraft the checklist."
    )
    assert person_facing_summary(None) is None


# ── checkpoint headers name their cause ──────────────────────────────────


def test_each_checkpoint_cause_has_its_own_header_with_the_shared_marker() -> None:
    from omnigent.context.rollover import CHECKPOINT_HEADERS, CHECKPOINT_MARKER, checkpoint_header

    size, back, manual = (checkpoint_header(c) for c in ("size", "return", "manual"))

    assert size == CHECKPOINT_HEADER  # the header stored before causes were named
    assert len({size, back, manual}) == 3
    assert "grew past its context limit" in size
    assert "came back after a long break" in back
    assert "asked to compact" in manual
    assert "grew past its context limit" not in back + manual
    assert (size, back, manual) == CHECKPOINT_HEADERS
    for header in CHECKPOINT_HEADERS:
        assert is_checkpoint_text(header) and header.startswith(CHECKPOINT_MARKER)
        assert "session_history" in header  # the rest of the framing is shared


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["size", "return", "manual"])
async def test_build_rollover_item_header_names_the_cause(cause: Any) -> None:
    from omnigent.context.rollover import checkpoint_header

    data = await build_rollover_item(
        _turn(1) + _turn(2),
        previous_summary=None,
        model="gpt-4o",
        llm_client=_ReturnsTextClient(_REPLY),
        cause=cause,
    )

    assert data.summary == f"{checkpoint_header(cause)}\n\n{_STATE}"
    assert data.compacted_messages is not None
    assert data.compacted_messages[0]["content"][0]["text"] == checkpoint_header(cause)


@pytest.mark.asyncio
async def test_build_rollover_item_defaults_to_the_size_header() -> None:
    data = await build_rollover_item(
        _turn(1),
        previous_summary=None,
        model="gpt-4o",
        llm_client=_ReturnsTextClient(_REPLY),
    )
    assert data.summary.startswith(CHECKPOINT_HEADER)


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["size", "return", "manual"])
async def test_a_later_rollover_strips_whatever_header_the_previous_one_had(cause: Any) -> None:
    from omnigent.context.rollover import checkpoint_header

    client = _ReturnsTextClient(_REPLY)
    await build_rollover_item(
        _turn(2),
        previous_summary=f"{checkpoint_header(cause)}\n\nOLD STATE",
        model="gpt-4o",
        llm_client=client,
    )

    prompt = str(client.seen_messages)
    assert "OLD STATE" in prompt
    assert "Context checkpoint inserted by the system" not in prompt


@pytest.mark.parametrize("cause", ["size", "return", "manual"])
def test_person_facing_summary_strips_every_cause_header(cause: Any) -> None:
    from omnigent.context.rollover import checkpoint_header

    assert person_facing_summary(f"{checkpoint_header(cause)}\n\nBody") == "Body"


def test_the_reset_checkpoint_keeps_its_own_wording() -> None:
    from omnigent.context.rollover import RESET_HEADER, build_reset_item

    item = build_reset_item("a9", model=None)

    assert item.summary == RESET_HEADER
    assert "cleared this conversation" in RESET_HEADER
    assert "grew past" not in RESET_HEADER
    assert person_facing_summary(RESET_HEADER) is None


# ── split_at_latest_compaction ────────────────────────────────────────────


def _split_item(item_id: str, item_type: str = "message", **extra: Any) -> dict[str, Any]:
    return {"id": item_id, "type": item_type, **extra}


def test_split_without_compaction_returns_everything() -> None:
    from omnigent.context.rollover import split_at_latest_compaction

    items = [_split_item("u1"), _split_item("a1")]
    assert split_at_latest_compaction(items) == (items, None)


def test_split_keeps_items_that_landed_between_anchor_and_checkpoint() -> None:
    from omnigent.context.rollover import split_at_latest_compaction

    ckpt = _split_item("c1", "compaction", last_item_id="a1")
    items = [
        _split_item("u1"),
        _split_item("a1"),
        _split_item("u2"),  # turn that ran while the rollover summarized
        _split_item("a2"),
        ckpt,
        _split_item("u3"),
    ]
    after, found = split_at_latest_compaction(items)
    assert found is ckpt
    assert [i["id"] for i in after] == ["u2", "a2", "u3"]


def test_split_with_unknown_anchor_falls_back_to_after_the_checkpoint() -> None:
    from omnigent.context.rollover import split_at_latest_compaction

    ckpt = _split_item("c1", "compaction", last_item_id="compact_boundary_x")
    items = [_split_item("u1"), ckpt, _split_item("u2")]
    after, _ = split_at_latest_compaction(items)
    assert [i["id"] for i in after] == ["u2"]


@pytest.mark.asyncio
async def test_summarizer_input_names_a_failed_call_and_leaves_out_tool_results() -> None:
    """A failed call reaches the summarizer as a ``Failed:`` line; results never do."""
    items = [
        *_turn(1),
        {
            "id": "i-call",
            "type": "function_call",
            "call_id": "c1",
            "name": "files_write",
            "arguments": '{"path": "projects/x/brief.md"}',
        },
        {"id": "i-out", "type": "function_call_output", "call_id": "c1", "output": "y" * 5_000},
        {
            "id": "i-call2",
            "type": "function_call",
            "call_id": "c2",
            "name": "web_fetch",
            "arguments": "{}",
        },
        {
            "id": "i-out2",
            "type": "function_call_output",
            "call_id": "c2",
            "output": '{"error": "404 not found\\nsecond line"}',
        },
    ]
    client = _ReturnsTextClient(_REPLY)
    await build_rollover_item(items, previous_summary=None, model="gpt-4o", llm_client=client)
    assert client.seen_messages is not None
    transcript = client.seen_messages[0]["content"][0]["text"]
    assert "Failed: web_fetch: 404 not found" in transcript
    assert "second line" not in transcript
    assert "y" * 50 not in transcript
    assert "TOOL" not in transcript


@pytest.mark.asyncio
async def test_placeholder_previous_summary_is_not_fed_back_as_state() -> None:
    """A harness's one-line compaction stand-in holds no state; it is not the previous summary."""
    placeholder = "[Claude Code compaction — context was automatically compacted]"
    client = _ReturnsTextClient(_REPLY)
    await build_rollover_item(
        _turn(1),
        previous_summary=placeholder,
        model="gpt-4o",
        llm_client=client,
    )
    assert client.seen_messages is not None
    texts = [
        block["text"]
        for message in client.seen_messages
        if message.get("role") == "user"
        for block in message["content"]
        if isinstance(block, dict) and "text" in block
    ]
    assert placeholder not in texts[0]


def test_is_placeholder_summary_only_matches_the_stand_in() -> None:
    from omnigent.context.rollover import is_placeholder_summary

    assert is_placeholder_summary("[Claude Code compaction — context was automatically compacted]")
    assert not is_placeholder_summary(None)
    assert not is_placeholder_summary("## Context checkpoint\n\nreal state")


# ── tagged state file + journal ──────────────────────────────────────────

_TAGGED_PREVIOUS = (
    f"{CHECKPOINT_HEADER}\n\n## Topics\n"
    "- [t-launch-budget · 2026-10-09] Spring launch budget: confirm the venue line\n"
    "- [o-venue · 2026-10-09] Venue still open\n"
    "Current position: waiting."
)


def test_summarizer_instruction_carries_the_tag_and_failure_rules() -> None:
    text = state_file_summarizer_instruction()
    assert "Keep every existing tag exactly" in text
    assert "closed:" in text and "merged into" in text
    assert "no tags, give tags" in text
    assert "tried and failed:" in text
    assert "anchored merge" in text
    assert "<journal>" in text
    assert "<journal>" not in state_file_summarizer_instruction(journal=False)


@pytest.mark.asyncio
async def test_the_journal_section_is_split_off_and_stored_on_the_item() -> None:
    reply = (
        "## Context checkpoint — 2026-10-12\n- [t-a] Alpha\n\n"
        "<journal>\n- 2026-10-12 · asked for alpha\n- 2026-10-12 · wrote a file\n</journal>"
    )
    data = await build_rollover_item(
        _turn(1) + _turn(2),
        previous_summary=None,
        model="gpt-4o",
        llm_client=_ReturnsTextClient(reply),
    )
    assert data.journal_lines == ["2026-10-12 · asked for alpha", "2026-10-12 · wrote a file"]
    assert "journal" not in data.summary
    assert all("journal" not in str(m) for m in data.compacted_messages)
    assert "[t-a" in data.summary
    assert data.repairs == 0 and data.evicted_entries == [] and data.applied_closes == []


def _write_call(call_id: str, path: str, response_id: str) -> list[dict[str, Any]]:
    call, output = _tool_pair(call_id, response_id)
    call.update(name="sys_os_write", arguments=f'{{"path": "{path}"}}')
    return [call, output]


@pytest.mark.asyncio
async def test_the_window_refs_come_from_code_and_each_turn_shows_its_own() -> None:
    items = (
        _turn(1)[:1]
        + _write_call("c1", "projects/q3/budget.xlsx", "r1")
        + _turn(1)[1:]
        + _turn(2)[:1]
        + _write_call("c2", "memo.md", "r2")
        + _turn(2)[1:]
        + _turn(3)
    )
    client = _ReturnsTextClient(
        "## Context checkpoint\n- [t-a] Alpha\n\n<journal>\n- 2026-10-12 · did it\n</journal>"
    )
    data = await build_rollover_item(
        items, previous_summary=None, model="gpt-4o", llm_client=client
    )

    assert data.journal_refs == {
        "files": ["projects/q3/budget.xlsx", "memo.md"],
        "project": ["q3"],
    }
    assert client.seen_messages is not None
    transcript = client.seen_messages[0]["content"][0]["text"]
    turns = transcript.split("They said: ")[1:]
    assert "REFS: files:projects/q3/budget.xlsx project:q3" in turns[0]
    assert "REFS: files:memo.md\n" in turns[1]
    assert "REFS:" not in turns[2]


@pytest.mark.asyncio
async def test_a_window_without_tool_calls_has_no_refs() -> None:
    data = await build_rollover_item(
        _turn(1),
        previous_summary=None,
        model="gpt-4o",
        llm_client=_ReturnsTextClient(_REPLY),
    )
    assert data.journal_refs is None


@pytest.mark.asyncio
async def test_a_reply_without_a_journal_is_refused() -> None:
    with pytest.raises(ValueError, match="no tagged entries or no journal"):
        await build_rollover_item(
            _turn(1),
            previous_summary=None,
            model="gpt-4o",
            llm_client=_ReturnsTextClient("## State\n- [t-a] Alpha"),
        )


@pytest.mark.asyncio
async def test_a_rewrite_that_loses_a_tag_is_repaired_and_counted() -> None:
    reply = (
        "## Topics\n- [t-launch-budget · 2026-10-12] Budget moved on\nCurrent position: x\n"
        "<journal>\n2026-10-12 · moved\n</journal>"
    )
    data = await build_rollover_item(
        _turn(1),
        previous_summary=_TAGGED_PREVIOUS,
        model="gpt-4o",
        llm_client=_ReturnsTextClient(reply),
    )
    assert data.repairs == 1
    assert "- [o-venue · 2026-10-09] Venue still open" in data.summary
    assert "Budget moved on" in data.summary
    restored = data.compacted_messages[1]["content"][0]["text"]
    assert "- [o-venue · 2026-10-09] Venue still open" in restored


@pytest.mark.asyncio
async def test_an_untagged_previous_checkpoint_is_carried_over_without_repairs() -> None:
    previous = (
        f"{CHECKPOINT_HEADER}\n\n## Topics\nSpring launch budget: venue line\nCurrent position: x"
    )
    data = await build_rollover_item(
        _turn(1),
        previous_summary=previous,
        model="gpt-4o",
        llm_client=_ReturnsTextClient(
            "- [t-launch] Spring launch budget: venue line\n<journal>\n2026-10-12 · x\n</journal>"
        ),
    )
    assert data.repairs == 0


@pytest.mark.asyncio
async def test_pending_closes_close_the_entry_and_reach_the_journal_lines() -> None:
    data = await build_rollover_item(
        _turn(1),
        previous_summary=_TAGGED_PREVIOUS,
        model="gpt-4o",
        llm_client=_ReturnsTextClient(
            "- [t-launch-budget · 2026-10-09] Spring launch budget\n"
            "<journal>\n2026-10-12 · talked\n</journal>"
        ),
        pending_closes={"o-venue": "the person said it is wrong"},
    )
    assert data.applied_closes == [{"tag": "o-venue", "reason": "the person said it is wrong"}]
    assert "] closed: Venue still open" in data.summary
    assert data.repairs == 0
    assert data.journal_lines[0] == "2026-10-12 · talked"
    assert data.journal_lines[-1].endswith("closed [o-venue]: the person said it is wrong")


@pytest.mark.asyncio
async def test_the_previous_state_fed_back_has_no_journal_and_no_header() -> None:
    client = _ReturnsTextClient("- [t-a] A\n<journal>\n2026-10-12 · x\n</journal>")
    first = await build_rollover_item(
        _turn(1), previous_summary=None, model="gpt-4o", llm_client=client
    )
    await build_rollover_item(
        _turn(2), previous_summary=first.summary, model="gpt-4o", llm_client=client
    )
    fed = client.seen_messages[0]["content"][0]["text"]
    assert "- [t-a · " in fed
    assert "2026-10-12 · x" not in fed


def test_old_compaction_items_load_and_render() -> None:
    from omnigent.entities import CompactionData
    from omnigent.entities.conversation import parse_item_data

    old = {
        "summary": f"{CHECKPOINT_HEADER}\n\nOLD STATE FILE with no tags",
        "last_item_id": "a1",
        "model": "gpt-4o",
        "token_count": 12,
        "compacted_messages": [],
    }
    data = parse_item_data("compaction", old)
    assert isinstance(data, CompactionData)
    assert data.journal_lines is None and data.repairs is None
    assert data.evicted_entries is None and data.applied_closes is None
    assert person_facing_summary(data.summary) == "OLD STATE FILE with no tags"
    # None fields stay out of the API shape, so old and new clients see old items unchanged.
    assert "journal_lines" not in data.model_dump(exclude_none=True)


def test_likely_duplicate_entries_are_pointed_out_to_the_summarizer() -> None:
    previous = (
        "## Context checkpoint\n"
        "- [t-venue · 2026-10-01] Venue for the spring launch: choosing between two halls\n"
        "- [t-hall · 2026-10-05] Spring launch venue: choosing between two halls downtown\n"
    )
    text = str(fold_input([_msg("m1", "user", "hello")], previous, now="2026-10-12 10:00 UTC"))
    assert "[t-venue] and [t-hall] may be the same thing" in text
    assert text.index("may be the same thing") < text.index("<turns>")
