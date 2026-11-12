"""Unit tests for ``omnigent.superchat.rollover``: the idle-refresh setting, the
compaction-item build+post (the fold of the turns before the kept tail), and single-flight.

No DB, no server, no live calls — tiny in-memory fakes stand in for the
server client and the LLM summarizer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.superchat import rollover as rollover_mod
from omnigent.superchat.rollover import (
    DEFAULT_IDLE_REFRESH_SECONDS,
    HARD_CONTEXT_WINDOW_FRACTION,
    OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV,
    exceeds_hard_context_limit,
    resolve_idle_refresh_seconds,
    roll_over_session,
)

_SUPERSIDE = {"omnigent.context.mode": "superside-chat"}
_ROLLOVER_MODE = {"omnigent.context.mode": "rollover"}
# A tail budget of 1 token keeps just the newest turn, so earlier turns are folded.
_FOLD = {**_SUPERSIDE, "omnigent.context.rollover_keep_tokens": "1"}
_REPLY = "- [t-a · 2026-10-12] Alpha\n<journal>\n2026-10-12 · hi\n</journal>"


@pytest.fixture(autouse=True)
def _clear_in_flight() -> None:
    rollover_mod._in_flight.clear()


# ── resolve_idle_refresh_seconds ──────────────────────────────────────────


def test_idle_refresh_seconds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, raising=False)
    assert resolve_idle_refresh_seconds() == DEFAULT_IDLE_REFRESH_SECONDS == 43_200


def test_idle_refresh_seconds_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, "5")
    assert resolve_idle_refresh_seconds() == 5


def test_idle_refresh_seconds_ignores_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, "not-a-number")
    assert resolve_idle_refresh_seconds() == DEFAULT_IDLE_REFRESH_SECONDS
    monkeypatch.setenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, "-1")
    assert resolve_idle_refresh_seconds() == DEFAULT_IDLE_REFRESH_SECONDS


# ── roll_over_session: fakes ──────────────────────────────────────────────


@dataclass
class _FakeResponse:
    status_code: int
    _body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self._body


@dataclass
class _FakeServerClient:
    """The items route in memory: ``type``, ``order``, ``limit`` and the exclusive
    ``after`` cursor (400 for an unknown id), plus a recording POST."""

    items: list[dict[str, Any]]
    post_status: int = 200
    get_status: int = 200
    posted: list[dict[str, Any]] = field(default_factory=list)
    patched: list[dict[str, Any]] = field(default_factory=list)
    get_params: list[dict[str, str]] = field(default_factory=list)

    @property
    def get_calls(self) -> int:
        return len(self.get_params)

    async def get(self, _url: str, *, params: dict[str, str], timeout: float) -> _FakeResponse:
        self.get_params.append(dict(params))
        if self.get_status != 200:
            return _FakeResponse(self.get_status, {})
        order = [it["id"] for it in self.items]
        rows = [it for it in self.items if params.get("type") in (None, it["type"])]
        if params.get("after"):
            if params["after"] not in order:
                return _FakeResponse(400, {})
            position = order.index(params["after"])
            desc = params.get("order") == "desc"
            rows = [
                it
                for it in rows
                if (order.index(it["id"]) < position if desc else order.index(it["id"]) > position)
            ]
        if params.get("order") == "desc":
            rows = rows[::-1]
        limit = int(params.get("limit", "100"))
        return _FakeResponse(200, {"data": rows[:limit], "has_more": len(rows) > limit})

    async def post(self, _url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        self.posted.append(json)
        return _FakeResponse(self.post_status, {})

    async def patch(self, _url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        self.patched.append(json)
        return _FakeResponse(200, {})


class _ReturnsTextClient:
    """LLM client stub returning a fixed summary (mirrors test_rollover.py)."""

    def __init__(self, text: str) -> None:
        self._text = text

    class _Responses:
        def __init__(self, outer: _ReturnsTextClient) -> None:
            self._outer = outer

        async def create(self, **_kwargs: Any) -> Response:
            return Response(
                output=[MessageOutput(content=[OutputText(text=self._outer._text)])],
                model="test-model",
            )

    @property
    def responses(self) -> _ReturnsTextClient._Responses:
        return self._Responses(self)


def _msg(item_id: str, role: str, text: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "response_id": f"resp_{item_id}",
        "created_at": 1,
        "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
    }


def _two_turns() -> list[dict[str, Any]]:
    return [
        _msg("u0", "user", "hi"),
        _msg("a0", "assistant", "hello"),
        _msg("u1", "user", "and then"),
        _msg("a1", "assistant", "sure"),
    ]


def _compaction_item(item_id: str, *, summary: str, last_item_id: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "compaction",
        "status": "completed",
        "response_id": f"resp_{item_id}",
        "created_at": 1,
        "summary": summary,
        "last_item_id": last_item_id,
        "model": "gpt-4o",
        "token_count": 10,
    }


# ── roll_over_session ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_roll_over_session_builds_and_posts_compaction_item() -> None:
    items = _two_turns()
    client = _FakeServerClient(items=items)
    rolled_over = []

    async def on_rolled_over() -> None:
        rolled_over.append(True)

    ok = await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
        on_rolled_over=on_rolled_over,
    )

    assert ok is True
    assert len(client.posted) == 1
    posted = client.posted[0]
    assert posted["type"] == "compaction"
    assert posted["data"]["type"] == "compaction"
    assert "Alpha" in posted["data"]["summary"]
    assert posted["data"]["last_item_id"] == "a0"  # only the turn before the kept tail
    # No prior compaction item exists: the single-flight guard still names
    # that expectation explicitly (``None``), not by omitting the field.
    assert posted["data"]["expected_previous_compaction_id"] is None
    assert rolled_over == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", [None, "return", "manual"])
async def test_roll_over_session_header_names_the_cause(cause: str | None) -> None:
    from omnigent.context.rollover import checkpoint_header

    client = _FakeServerClient(items=_two_turns())
    extra: dict[str, Any] = {} if cause is None else {"cause": cause}

    ok = await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
        **extra,
    )

    assert ok is True
    summary = client.posted[0]["data"]["summary"]
    assert summary.startswith(checkpoint_header(cause or "size"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_roll_over_session_resumes_from_previous_compaction() -> None:
    """Items at/before the latest compaction item are excluded from the new fold."""
    items = [
        _msg("u1", "user", "earlier, already compacted"),
        _compaction_item("c1", summary="OLD SUMMARY", last_item_id="u1"),
        _msg("u2", "user", "new question"),
        _msg("a2", "assistant", "new answer"),
        _msg("u3", "user", "newest question"),
        _msg("a3", "assistant", "newest answer"),
    ]
    client = _FakeServerClient(items=items)

    ok = await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
    )

    assert ok is True
    posted_data = client.posted[0]["data"]
    # Anchored on the last folded item: the newest turn stays after it.
    assert posted_data["last_item_id"] == "a2"
    # Named the checkpoint it actually summarized from, for the server's
    # single-flight guard.
    assert posted_data["expected_previous_compaction_id"] == "c1"


@pytest.mark.asyncio
async def test_roll_over_session_noop_when_nothing_since_last_compaction() -> None:
    items = [
        _msg("u1", "user", "hi"),
        _compaction_item("c1", summary="OLD SUMMARY", last_item_id="u1"),
    ]
    client = _FakeServerClient(items=items)

    ok = await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
    )

    assert ok is False
    assert client.posted == []


@pytest.mark.asyncio
async def test_roll_over_session_post_failure_is_non_fatal_and_skips_callback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-2xx POST never calls back: the new compaction item was NOT
    durably persisted, so the warm client must not be dropped on top of it
    (that would cold-start the next turn with no rollover item to read)."""
    items = _two_turns()
    client = _FakeServerClient(items=items, post_status=500)
    rolled_over = []

    async def on_rolled_over() -> None:
        rolled_over.append(True)

    with caplog.at_level("WARNING"):
        ok = await roll_over_session(
            "conv_1",
            labels=_FOLD,
            model="gpt-4o",
            server_client=client,  # type: ignore[arg-type]
            llm_client=_ReturnsTextClient(_REPLY),
            on_rolled_over=on_rolled_over,
        )

    assert ok is False
    assert rolled_over == []
    assert any("500" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_roll_over_session_conflict_is_a_quiet_lost_race() -> None:
    """A 409 means another process already rolled this session over — not
    a failure: no warning, just ``False`` and no callback."""
    items = _two_turns()
    client = _FakeServerClient(items=items, post_status=409)
    rolled_over = []

    async def on_rolled_over() -> None:
        rolled_over.append(True)

    ok = await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
        on_rolled_over=on_rolled_over,
    )

    assert ok is False
    assert rolled_over == []


@pytest.mark.asyncio
async def test_roll_over_session_get_items_failure_is_non_fatal() -> None:
    """A non-200 page while scanning for the rollover must raise (caught and
    logged by the caller), not silently stop with a partial item range that
    would summarize over a hole in the history."""
    items = _two_turns()
    client = _FakeServerClient(items=items, get_status=503)

    ok = await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
    )

    assert ok is False
    assert client.posted == []


@pytest.mark.asyncio
async def test_roll_over_session_single_flight() -> None:
    items = _two_turns()
    client = _FakeServerClient(items=items)
    rollover_mod._in_flight.add("conv_1")

    ok = await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
    )

    assert ok is False
    assert client.get_calls == 0
    assert client.posted == []


@pytest.mark.asyncio
async def test_roll_over_session_clears_in_flight_after_completion() -> None:
    items = _two_turns()
    client = _FakeServerClient(items=items)

    await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
    )

    assert "conv_1" not in rollover_mod._in_flight


# ── hard context limit ────────────────────────────────────────────────────


def test_hard_limit_needs_a_known_window_and_tokens() -> None:
    window_label = {"omnigent.last_context_window": "200000"}
    assert not exceeds_hard_context_limit(window_label, context_tokens=None)
    assert not exceeds_hard_context_limit(_SUPERSIDE, context_tokens=10_000_000)


def test_hard_limit_boundary_is_the_hard_fraction_of_the_window() -> None:
    labels = {"omnigent.last_context_window": "200000"}
    limit = int(200_000 * HARD_CONTEXT_WINDOW_FRACTION)
    assert not exceeds_hard_context_limit(labels, context_tokens=limit - 1)
    assert exceeds_hard_context_limit(labels, context_tokens=limit)


# ── a turn that ran during the rollover stays in context ──────────────────


@pytest.mark.asyncio
async def test_turn_during_rollover_is_not_covered_by_the_checkpoint() -> None:
    """Items between the anchor and the (later-posted) checkpoint still count.

    The rollover anchored on ``a1``; ``u2``/``a2`` landed while it summarized,
    so the compaction item sits after them. The next rollover must summarize
    them, not treat them as already covered.
    """
    items = [
        _msg("u1", "user", "first"),
        _msg("a1", "assistant", "first answer"),
        _msg("u2", "user", "asked during the rollover"),
        _msg("a2", "assistant", "answered during the rollover"),
        _msg("u3", "user", "the newest turn"),
        _msg("a3", "assistant", "its answer"),
        _compaction_item("c1", summary="OLD SUMMARY", last_item_id="a1"),
    ]
    client = _FakeServerClient(items=items)

    ok = await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
    )

    assert ok is True
    posted_data = client.posted[0]["data"]
    assert posted_data["last_item_id"] == "a2"
    assert posted_data["expected_previous_compaction_id"] == "c1"


def test_summarizer_model_routes_a_bare_claude_model_to_anthropic() -> None:
    """A harness reports its model bare; the summarizer must not send it as an OpenAI call."""
    from omnigent.superchat.rollover import summarizer_model

    anthropic = {"base_url": "https://api.anthropic.com"}
    assert summarizer_model("claude-sonnet-5-5", anthropic) == "anthropic/claude-sonnet-5-5"
    assert summarizer_model("claude-haiku-4-5", None) == "anthropic/claude-haiku-4-5"
    assert summarizer_model("some-model", {"base_url": "https://api.anthropic.com/v1"}) == (
        "anthropic/some-model"
    )
    assert summarizer_model("gpt-5", None) == "gpt-5"
    assert summarizer_model("openrouter/anthropic/claude-sonnet-5-5", anthropic) == (
        "openrouter/anthropic/claude-sonnet-5-5"
    )


def test_summarizer_model_follows_the_connection_wire_not_the_name() -> None:
    """With the session's connection known, its wire decides whatever the model is called."""
    from omnigent.superchat.rollover import summarizer_model

    assert summarizer_model("team-model", None, "anthropic") == "anthropic/team-model"
    assert summarizer_model("claude-sonnet-5-5", None, "openai_compatible") == (
        "openai_compatible/claude-sonnet-5-5"
    )
    assert summarizer_model("deepseek/deepseek-chat", None, "openai_compatible") == (
        "openai_compatible/deepseek/deepseek-chat"
    )


class _RecordingClient(_ReturnsTextClient):
    """Returns a summary and records what each model call was made with."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.calls: list[dict[str, Any]] = []

    @property
    def responses(self) -> Any:
        outer = self

        class _Responses:
            async def create(self, **kwargs: Any) -> Response:
                outer.calls.append(kwargs)
                return Response(
                    output=[MessageOutput(content=[OutputText(text=outer._text)])],
                    model="test-model",
                )

        return _Responses()


@pytest.mark.asyncio
async def test_a_non_claude_model_on_an_anthropic_wire_connection_is_summarized_over_it() -> None:
    """``team-model`` on a session whose connection speaks Anthropic Messages (a LiteLLM proxy
    through the engine's model proxy) is summarized as an Anthropic call on that connection."""
    proxy = {"base_url": "https://engine.example/v1/model/litellm/v1", "api_key": "host-a:tok"}
    client = _FakeServerClient(items=_two_turns())
    llm = _RecordingClient(_REPLY)
    ok = await roll_over_session(
        "conv_wire",
        labels=_FOLD,
        model="team-model",
        server_client=client,  # type: ignore[arg-type]
        llm_client=llm,
        connection=proxy,
        wire="anthropic",
    )
    assert ok is True and llm.calls
    assert {call["model"] for call in llm.calls} == {"anthropic/team-model"}
    assert all(proxy["base_url"] in str(call) for call in llm.calls)


# ── anchored reads: only the record from the checkpoint's anchor on ───────


def _long_record(*, before: int, during: int, after: int) -> list[dict[str, Any]]:
    """*before* summarized items, a checkpoint anchored on the last of them, the
    *during* items that landed while it summarized, the checkpoint, then *after* items."""
    items = [_msg(f"m{i}", "user" if i % 2 == 0 else "assistant", f"t{i}") for i in range(before)]
    anchor = items[-1]["id"]
    items += [_msg(f"d{i}", "user" if i % 2 == 0 else "assistant", f"d{i}") for i in range(during)]
    items.append(_compaction_item("c1", summary="REAL SUMMARY", last_item_id=anchor))
    items += [_msg(f"n{i}", "user" if i % 2 == 0 else "assistant", f"n{i}") for i in range(after)]
    return items


def _full_read_result(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any, Any]:
    from omnigent.context.rollover import split_at_latest_compaction

    since, checkpoint = split_at_latest_compaction(items)
    if checkpoint is None:
        return items, None, None
    return since, checkpoint["summary"], checkpoint["id"]


@pytest.mark.asyncio
async def test_rollover_reads_only_from_the_anchor_on_a_long_record() -> None:
    items = _long_record(before=5000, during=4, after=6)
    client = _FakeServerClient(items=items)

    result = await rollover_mod._fetch_items_since_last_compaction(
        client,  # type: ignore[arg-type]
        "conv_1",
    )

    assert result == _full_read_result(items)
    assert client.get_params == [
        {"limit": "20", "order": "desc", "type": "compaction"},
        {"limit": "1000", "order": "asc", "after": "m4999"},
    ]


@pytest.mark.asyncio
async def test_anchored_read_pages_with_the_page_cursor() -> None:
    items = _long_record(before=3000, during=0, after=2200)
    client = _FakeServerClient(items=items)

    result = await rollover_mod._fetch_items_since_last_compaction(
        client,  # type: ignore[arg-type]
        "conv_1",
    )

    assert result == _full_read_result(items)
    assert [p.get("after") for p in client.get_params[1:]] == ["m2999", "n998", "n1998"]
    assert {p["limit"] for p in client.get_params[1:]} == {"1000"}


@pytest.mark.asyncio
async def test_a_record_without_a_checkpoint_is_read_from_the_start() -> None:
    items = [_msg(f"m{i}", "user" if i % 2 == 0 else "assistant", "x") for i in range(40)]
    client = _FakeServerClient(items=items)

    result = await rollover_mod._fetch_items_since_last_compaction(
        client,  # type: ignore[arg-type]
        "conv_1",
    )

    assert result == (items, None, None)
    assert client.get_params[1] == {"limit": "1000", "order": "asc"}


@pytest.mark.asyncio
async def test_an_anchor_the_record_does_not_hold_reads_after_the_checkpoint() -> None:
    """A native forwarder's synthetic boundary id: same result as the full read."""
    items = _long_record(before=30, during=2, after=3)
    items[32]["last_item_id"] = "boundary_not_in_the_record"
    client = _FakeServerClient(items=items)

    result = await rollover_mod._fetch_items_since_last_compaction(
        client,  # type: ignore[arg-type]
        "conv_1",
    )

    assert result == _full_read_result(items)
    assert [p.get("after") for p in client.get_params[1:]] == [
        "boundary_not_in_the_record",
        "c1",
    ]


@pytest.mark.asyncio
async def test_a_placeholder_checkpoint_is_not_state_the_previous_real_one_is() -> None:
    """A stand-in checkpoint (a manual SDK /compact before the fix) is skipped: the
    summary comes from the previous real checkpoint and the turns the stand-in
    covered are summarized again."""
    placeholder = "[Claude Code compaction — context was automatically compacted]"
    items = [
        _msg("u1", "user", "old"),
        _msg("a1", "assistant", "old answer"),
        _compaction_item("c1", summary="REAL SUMMARY", last_item_id="a1"),
        _msg("u2", "user", "kept by the placeholder"),
        _msg("a2", "assistant", "answer"),
        _compaction_item("c2", summary=placeholder, last_item_id="a2"),
        _msg("u3", "user", "after"),
    ]
    client = _FakeServerClient(items=items)

    since, summary, checkpoint_id = await rollover_mod._fetch_items_since_last_compaction(
        client,  # type: ignore[arg-type]
        "conv_1",
    )

    assert (summary, checkpoint_id) == ("REAL SUMMARY", "c1")
    assert [it["id"] for it in since] == ["u2", "a2", "u3"]


@pytest.mark.asyncio
async def test_a_record_with_only_placeholder_checkpoints_is_read_from_the_start() -> None:
    placeholder = "[Claude Code compaction — context was automatically compacted]"
    items = [
        _msg("u1", "user", "hi"),
        _compaction_item("c1", summary=placeholder, last_item_id="u1"),
        _msg("u2", "user", "again"),
    ]
    client = _FakeServerClient(items=items)

    since, summary, checkpoint_id = await rollover_mod._fetch_items_since_last_compaction(
        client,  # type: ignore[arg-type]
        "conv_1",
    )

    assert (summary, checkpoint_id) == (None, None)
    assert [it["id"] for it in since] == ["u1", "u2"]


# ── state_close: pending closes at the next checkpoint ──────────────────────

_TAGGED = (
    "## Topics\n"
    "- [t-launch-budget · 2026-10-09] Spring launch budget\n"
    "- [o-venue · 2026-10-09] Venue still open\n"
    "- [d-vendor · 2026-10-09] Vendor chosen"
)


def _closes(**reasons: str) -> dict[str, str]:
    return {
        **_FOLD,
        **{f"omnigent.state_close.{tag.replace('_', '-')}": why for tag, why in reasons.items()},
    }


@pytest.mark.asyncio
async def test_two_pending_closes_both_apply_are_journaled_and_cleared() -> None:
    from omnigent.context.rollover import checkpoint_header

    items = [
        _msg("u1", "user", "hi"),
        _compaction_item("c1", summary=f"{checkpoint_header()}\n\n{_TAGGED}", last_item_id="u1"),
        _msg("u2", "user", "forget the venue and the vendor"),
        _msg("a2", "assistant", "done"),
        _msg("u3", "user", "thanks"),
        _msg("a3", "assistant", "welcome"),
    ]
    client = _FakeServerClient(items=items)
    # The summarizer drops the venue and keeps the rest: the close must not be undone by repair.
    reply = (
        "- [t-launch-budget · 2026-10-09] Spring launch budget\n"
        "- [d-vendor · 2026-10-09] Vendor chosen\n<journal>\n2026-10-12 · talked\n</journal>"
    )

    ok = await roll_over_session(
        "conv_1",
        labels=_closes(o_venue="wrong", d_vendor="person changed their mind"),
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(reply),
    )

    assert ok is True
    data = client.posted[0]["data"]
    assert "] closed: Venue still open" in data["summary"]
    assert "] closed: Vendor chosen" in data["summary"]
    assert data["repairs"] == 0
    assert {c["tag"]: c["reason"] for c in data["applied_closes"]} == {
        "o-venue": "wrong",
        "d-vendor": "person changed their mind",
    }
    assert any("closed [o-venue]: wrong" in line for line in data["journal_lines"])
    assert any("closed [d-vendor]: person changed their mind" in x for x in data["journal_lines"])
    assert client.patched == [
        {"labels": {"omnigent.state_close.o-venue": "", "omnigent.state_close.d-vendor": ""}}
    ]


@pytest.mark.asyncio
async def test_a_failed_post_leaves_the_closes_pending() -> None:
    client = _FakeServerClient(items=_two_turns(), post_status=500)
    await roll_over_session(
        "conv_1",
        labels=_closes(o_venue="wrong"),
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
    )
    assert client.patched == []


@pytest.mark.asyncio
async def test_the_posted_body_carries_the_new_payload_fields() -> None:
    client = _FakeServerClient(items=_two_turns())
    await roll_over_session(
        "conv_1",
        labels=_FOLD,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient(_REPLY),
    )
    data = client.posted[0]["data"]
    assert data["journal_lines"] == ["2026-10-12 · hi"]
    assert data["evicted_entries"] == [] and data["repairs"] == 0
    assert client.patched == []


@dataclass
class _LabelsServerClient(_FakeServerClient):
    """The items route plus the labels route, whose labels can change mid-checkpoint."""

    labels: dict[str, str] = field(default_factory=dict)

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if url.endswith("/labels"):
            return _FakeResponse(200, {"labels": self.labels})
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_a_decision_is_written_and_cleared_unless_a_newer_one_came_meanwhile() -> None:
    key_a, key_b = "omnigent.state_add.d-q3-deck", "omnigent.state_add.d-budget"
    labels = {**_FOLD, key_a: "Q3 deck (deck): 8 slides, checked", key_b: "Budget: checked"}
    client = _LabelsServerClient(
        items=_two_turns(),
        # The Q3 deck was decided again while the summary was being written.
        labels={key_a: "Q3 deck (deck): 9 slides, checked", key_b: "Budget: checked"},
    )

    ok = await roll_over_session(
        "conv_1",
        labels=labels,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient("- [t-a] A\n<journal>\n2026-10-12 · hi\n</journal>"),
    )

    assert ok is True
    summary = client.posted[0]["data"]["summary"]
    assert "[d-q3-deck · " in summary and "Q3 deck (deck): 8 slides, checked" in summary
    assert "Budget: checked" in summary
    # Only the unchanged label is cleared; the newer decision waits for the next checkpoint.
    assert client.patched == [{"labels": {key_b: ""}}]


# ── state_close tool ────────────────────────────────────────────────────────


def _handler_ctx(client: Any) -> Any:
    from omnigent.superchat.feature import HandlerCtx

    return HandlerCtx("state_close", client, "conv_1", _SUPERSIDE)


def _checkpoint_client() -> _FakeServerClient:
    from omnigent.context.rollover import checkpoint_header

    return _FakeServerClient(
        items=[
            _msg("u1", "user", "hi"),
            _compaction_item(
                "c1",
                summary=f"{checkpoint_header()}\n\n{_TAGGED}\n- [t-old · 2026-10-01] closed: x",
                last_item_id="u1",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_state_close_records_a_label_per_tag() -> None:
    import json

    from omnigent.superchat.state_file.handlers import handle_state_close

    client = _checkpoint_client()
    for tag in ("[o-venue · 2026-10-09]", "d-vendor"):
        out = json.loads(
            await handle_state_close(_handler_ctx(client), {"tag": tag, "reason": "wrong"})
        )
        assert "next checkpoint" in out["status"]
    assert [p["labels"] for p in client.patched] == [
        {"omnigent.state_close.o-venue": "wrong"},
        {"omnigent.state_close.d-vendor": "wrong"},
    ]


@pytest.mark.asyncio
async def test_state_close_reports_unknown_and_already_closed_tags() -> None:
    import json

    from omnigent.superchat.state_file.handlers import handle_state_close

    client = _checkpoint_client()
    unknown = json.loads(
        await handle_state_close(_handler_ctx(client), {"tag": "t-nope", "reason": "x"})
    )
    assert "error" in unknown and "t-nope" in unknown["error"]
    closed = json.loads(
        await handle_state_close(_handler_ctx(client), {"tag": "t-old", "reason": "x"})
    )
    assert closed["status"] == "already closed"
    bad = json.loads(await handle_state_close(_handler_ctx(client), {"tag": "???", "reason": "x"}))
    assert "error" in bad
    assert client.patched == []


def test_state_close_is_offered_to_the_super_chat_only() -> None:
    from omnigent.superchat.feature import ToolManagerCtx
    from omnigent.superchat.features import FEATURE_HANDLERS, feature_tools

    def names(labels: dict[str, str]) -> set[str]:
        return {t.name() for t in feature_tools(labels, ToolManagerCtx())}

    assert "state_close" in FEATURE_HANDLERS
    assert "state_close" in names(_SUPERSIDE)
    assert "state_close" not in names({**_SUPERSIDE, "omnigent.subagent": "x"})
    assert "state_close" not in names(_ROLLOVER_MODE)
    assert "state_close" not in names({})
