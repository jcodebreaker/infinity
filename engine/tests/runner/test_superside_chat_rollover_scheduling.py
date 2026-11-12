"""Tests for the runner-side superside-chat session handling around the fold (ADR 0031):

1. A warm-client drop that would land while a DIFFERENT turn is active
   (``_drop_superside_chat_warm_client``) is deferred (recorded in
   ``_superside_chat_pending_refresh``) rather than releasing the harness
   subprocess out from under that turn, and is applied at the start of the
   session's next turn (``_consume_superside_chat_pending_refresh``).
2. The next turn never waits for a summary; at the hard context limit it drops the warm
   client and starts from the newest whole turns that fit.
3. A return after the idle period drops the warm client (``_superside_chat_handoff``), driven
   by the runner's clock of the last model request, and names the gap.

No DB, no server, no live LLM calls — the harness, the Omnigent server
client, and the summarizer LLM client are all tiny in-memory fakes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.context.rollover import BLOCK_FRAME, checkpoint_header
from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.runner import create_runner_app
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_SUPERSIDE_LABELS = {"omnigent.context.mode": "superside-chat"}


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.01
) -> None:
    """Poll *predicate* until it's true, or raise after *timeout* seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def _history_items() -> list[dict[str, Any]]:
    """One user/assistant turn, timestamped now (keeps idle-refresh quiet)."""
    now = int(time.time())
    return [
        {
            "id": "u1",
            "type": "message",
            "status": "completed",
            "response_id": "r1",
            "created_at": now,
            "role": "user",
            "content": [{"type": "input_text", "text": "question"}],
        },
        {
            "id": "a1",
            "type": "message",
            "status": "completed",
            "response_id": "r1",
            "created_at": now,
            "role": "assistant",
            "content": [{"type": "output_text", "text": "answer"}],
        },
    ]


class _ThresholdServerClient(NullServerClient):
    """Answers ``/labels`` and ``/items``; records posted compaction events."""

    def __init__(self, *, labels: dict[str, str], items: list[dict[str, Any]]) -> None:
        self._labels = labels
        self._items = items
        self.posted_compactions: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        labels, items = self._labels, self._items
        if url.endswith("/labels"):

            class _LabelsResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {"labels": labels}

            return _LabelsResponse()
        if url.endswith("/items"):

            class _ItemsResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {"data": items, "has_more": False}

            return _ItemsResponse()
        return await super().get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url.endswith("/events"):
            body = kwargs.get("json", {})
            if isinstance(body, dict) and body.get("type") == "compaction":
                self.posted_compactions.append(body)
        return await super().post(url, **kwargs)


class _GatedLLMClient:
    """Summarizer stub whose ``responses.create`` blocks on *gate*."""

    def __init__(self, gate: asyncio.Event, text: str = "ROLLING SUMMARY") -> None:
        self._gate = gate
        self._text = text
        self.call_count = 0

    class _Responses:
        def __init__(self, outer: _GatedLLMClient) -> None:
            self._outer = outer

        async def create(self, **_kwargs: Any) -> Response:
            self._outer.call_count += 1
            await self._outer._gate.wait()
            return Response(
                output=[MessageOutput(content=[OutputText(text=self._outer._text)])],
                model="test-model",
            )

    @property
    def responses(self) -> _GatedLLMClient._Responses:
        return self._Responses(self)


class _SequencedHarnessClient(_ScriptedHarnessClient):
    """Streams a different scripted SSE sequence for each successive turn."""

    def __init__(self, frame_sequences: list[list[str]]) -> None:
        super().__init__([])
        self._sequences = frame_sequences
        self.call_count = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)
        idx = min(self.call_count, len(self._sequences) - 1)
        frames = self._sequences[idx]
        self.call_count += 1

        class _Ctx:
            status_code = 200

            async def __aenter__(self_inner) -> Any:
                return _ScriptedHarnessClient._StreamHandle(frames, None)

            async def __aexit__(self_inner, *_: Any) -> None:
                return None

        return _Ctx()


def _triggering_frames(context_tokens: int) -> list[str]:
    return [
        _sse({"type": "response.created", "response": {"id": "resp_trigger"}}),
        _sse(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_trigger",
                    "usage": {"context_tokens": context_tokens, "model": "claude-test"},
                },
            }
        ),
    ]


def _plain_frames(resp_id: str = "resp_plain") -> list[str]:
    return [
        _sse({"type": "response.created", "response": {"id": resp_id}}),
        _sse(
            {
                "type": "response.completed",
                "response": {
                    "id": resp_id,
                    "usage": {"context_tokens": 10, "model": "claude-test"},
                },
            }
        ),
    ]


def _build_app(
    harness_client: _ScriptedHarnessClient, server_client: _ThresholdServerClient
) -> tuple[Any, _FakeProcessManager]:
    pm = _FakeProcessManager(harness_client)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, pm


async def _post_turn(client: Any, conv_id: str, text: str) -> Any:
    """POST a turn on the DEFAULT (background) path — no ``?stream=true``."""
    return await client.post(
        f"/v1/sessions/{conv_id}/events",
        json={
            "type": "message",
            "role": "user",
            "model": "test-agent",
            "content": [{"type": "input_text", "text": text}],
            "harness": "claude-sdk",
        },
    )


@pytest.mark.asyncio
async def test_release_deferred_while_turn_active_applied_at_next_turn_start() -> None:
    """A drop deferred by a live turn (a finished /compact) is applied at the next turn's start."""
    server_client = _ThresholdServerClient(labels=_SUPERSIDE_LABELS, items=_history_items())
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, pm = _build_app(harness_client, server_client)
    conv_id = "conv_defer"

    async with _runner_client(app) as client:
        resp = await asyncio.wait_for(_post_turn(client, conv_id, "hello"), timeout=5.0)
        assert resp.status_code == 202
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)

        # What the drop records when it lands while a turn is live.
        app.state.superside_chat_pending_refresh.add(conv_id)
        assert pm.released == []

        # The next turn applies the deferred drop at its start, before
        # dispatching to the harness.
        resp2 = await asyncio.wait_for(_post_turn(client, conv_id, "second"), timeout=5.0)
        assert resp2.status_code == 202
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)

    assert pm.released == [conv_id]
    assert conv_id not in app.state.superside_chat_pending_refresh


class _HardLimitServerClient(_ThresholdServerClient):
    """Also answers ``GET /v1/sessions/{id}`` with a model, so a rollover can be scheduled."""

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url.count("/") == 3 and url.startswith("/v1/sessions/"):

            class _SessionResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {"llm_model": "claude-test"}

            return _SessionResponse()
        return await super().get(url, **kwargs)


_KEEP_TOKENS = 1_500


def _long_history_items(turns: int = 30) -> list[dict[str, Any]]:
    """*turns* user/assistant turns of about 400 tokens each, timestamped now."""
    now = int(time.time())
    filler = "lorem ipsum dolor sit amet " * 40
    items: list[dict[str, Any]] = []
    for i in range(turns):
        for role, kind in (("user", "input_text"), ("assistant", "output_text")):
            items.append(
                {
                    "id": f"{role[0]}{i}",
                    "type": "message",
                    "status": "completed",
                    "response_id": f"r{i}",
                    "created_at": now,
                    "role": role,
                    "content": [{"type": kind, "text": f"turn {i} {filler}"}],
                }
            )
    return items


def _hard_limit_labels(**extra: str) -> dict[str, str]:
    return {
        **_SUPERSIDE_LABELS,
        "omnigent.last_context_window": "400000",
        "omnigent.context.rollover_keep_tokens": str(_KEEP_TOKENS),
        **extra,
    }


def _assert_trimmed_to_the_budget(harness_client: _ScriptedHarnessClient, text: str) -> None:
    """The last harness input is a few whole recent turns ending with *text*."""
    from omnigent.runtime.compaction import count_tokens

    content = harness_client.posted_bodies[-1]["content"]
    messages = [m for m in content if m.get("type") == "message"]
    assert len(messages) < 20, "the whole record was sent"
    assert messages[0]["role"] == "user", "a turn was cut in half"
    last_text = " ".join(b["text"] for b in messages[-1]["content"] if "text" in b)
    assert messages[-1]["role"] == "user" and text in last_text
    assert count_tokens(messages[:-1], "gpt-4o") <= _KEEP_TOKENS


@pytest.mark.asyncio
async def test_hard_limit_trims_and_drops_the_warm_client_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At/over the hard fraction of the window the next turn neither waits for a summary nor
    overflows: it cold-starts on the newest whole turns."""
    gate = asyncio.Event()  # never set: nothing may wait on the summarizer
    llm_client = _GatedLLMClient(gate)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)

    # 500k fill of a 400k window: dispatching the whole record would overflow.
    server_client = _HardLimitServerClient(
        labels=_hard_limit_labels(), items=_long_history_items()
    )
    harness_client = _SequencedHarnessClient([_triggering_frames(500_000), _plain_frames()])
    app, pm = _build_app(harness_client, server_client)
    conv_id = "conv_inflight"

    async with _runner_client(app) as client:
        await asyncio.wait_for(_post_turn(client, conv_id, "hello"), timeout=5.0)
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)

        await asyncio.wait_for(_post_turn(client, conv_id, "second question"), timeout=5.0)
        await _wait_until(lambda: len(harness_client.posted_bodies) == 2, timeout=2.0)
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)

        assert llm_client.call_count == 0
        assert server_client.posted_compactions == []
        _assert_trimmed_to_the_budget(harness_client, "second question")
        assert pm.released == [conv_id]


@pytest.mark.asyncio
async def test_hard_limit_known_only_from_the_labels_trims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fill is known only from the session's labels, as on a first turn after a restart."""
    gate = asyncio.Event()
    llm_client = _GatedLLMClient(gate)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)

    server_client = _HardLimitServerClient(
        labels=_hard_limit_labels(**{"omnigent.last_context_tokens": "390000"}),
        items=_long_history_items(),
    )
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, pm = _build_app(harness_client, server_client)
    conv_id = "conv_never_started"

    async with _runner_client(app) as client:
        await asyncio.wait_for(_post_turn(client, conv_id, "first question"), timeout=5.0)
        await _wait_until(lambda: len(harness_client.posted_bodies) == 1, timeout=2.0)
        _assert_trimmed_to_the_budget(harness_client, "first question")
        assert pm.released == [conv_id]
        assert llm_client.call_count == 0


@pytest.mark.asyncio
async def test_a_cold_start_below_the_hard_limit_replays_only_the_newest_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relaunched harness gets the checkpoint plus the newest whole turns that fit the tail
    budget, not the whole record, and no summarizer runs for it."""
    from omnigent.runtime.compaction import count_tokens

    llm_client = _GatedLLMClient(asyncio.Event())
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)
    # About 80k tokens since the last checkpoint, well under the hard limit.
    items = _long_history_items(turns=150)
    server_client = _HardLimitServerClient(
        labels={**_SUPERSIDE_LABELS, "omnigent.last_context_window": "1000000"}, items=items
    )
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, _pm = _build_app(harness_client, server_client)

    async with _runner_client(app) as client:
        await asyncio.wait_for(_post_turn(client, "conv_cold_tail", "hello"), timeout=5.0)
        await _wait_until(lambda: len(harness_client.posted_bodies) == 1, timeout=2.0)

    messages = [
        m for m in harness_client.posted_bodies[0]["content"] if m.get("type") == "message"
    ]
    assert messages[0]["role"] == "user", "a turn was cut in half"
    assert "turn 149" in " ".join(b["text"] for b in messages[-2]["content"] if "text" in b)
    assert count_tokens(messages, "gpt-4o") < 20_000
    assert llm_client.call_count == 0


class _FlakyThenSuccessLabelsServerClient(_ThresholdServerClient):
    """Every ``/labels`` lookup fails (502) while ``self.failing`` is set;
    all succeed once a test clears it.

    Reproduces a transient label-fetch outage spanning a superside-chat
    session's entire first turn (every lookup that turn makes fails, not
    just one), to prove the runner's rollover-labels cache does not
    permanently remember that outage as "not superside-chat" (the bug: the
    old ``_session_labels_for_runner_spawn`` returned ``{}`` on every
    failure, indistinguishable from a confirmed-empty label set, and that
    ``{}`` got cached forever).
    """

    def __init__(self, *, labels: dict[str, str], items: list[dict[str, Any]]):
        super().__init__(labels=labels, items=items)
        self.failing = True
        self.labels_calls = 0

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url.endswith("/labels"):
            self.labels_calls += 1
            if self.failing:

                class _FailResponse(NullServerClient._Response):
                    status_code = 502

                return _FailResponse()
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_transient_labels_failure_does_not_poison_the_rollover_cache() -> None:
    """A session that looked up as "unknown" on one turn must be re-checked
    (not permanently treated as non-superside-chat) on the next."""
    server_client = _FlakyThenSuccessLabelsServerClient(
        labels=_hard_limit_labels(**{"omnigent.last_context_tokens": "390000"}),
        items=_long_history_items(),
    )
    harness_client = _SequencedHarnessClient([_triggering_frames(500_000), _plain_frames()])
    app, pm = _build_app(harness_client, server_client)
    conv_id = "conv_flaky_labels"

    async with _runner_client(app) as client:
        # First turn: every /labels lookup for this session fails, so the
        # turn must proceed as a non-superside-chat turn — no hard-limit drop.
        resp1 = await asyncio.wait_for(_post_turn(client, conv_id, "hello"), timeout=5.0)
        assert resp1.status_code == 202
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)
        assert pm.released == []

        # The outage recovers before the next turn.
        server_client.failing = False

        # Second turn: the lookup now succeeds. If the first turn's failures
        # had been cached as "confirmed non-superside-chat", the hard limit
        # would be skipped — it must not be.
        resp2 = await asyncio.wait_for(_post_turn(client, conv_id, "again"), timeout=5.0)
        assert resp2.status_code == 202
        await _wait_until(lambda: len(harness_client.posted_bodies) == 2, timeout=2.0)
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)

        _assert_trimmed_to_the_budget(harness_client, "again")
        assert pm.released == [conv_id]


# ── ?stream=true is refused for superside-chat sessions ──────────────────


@pytest.mark.asyncio
async def test_stream_true_is_refused_for_superside_chat_session() -> None:
    """The direct-dispatch ``?stream=true`` path never threads context_mode,
    the Memory Profile block, or the idle-refresh/pending-refresh hooks, so
    it must refuse a superside-chat turn rather than silently run it without
    them."""
    server_client = _ThresholdServerClient(labels=_SUPERSIDE_LABELS, items=_history_items())
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, _pm = _build_app(harness_client, server_client)
    conv_id = "conv_stream_refused"

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hello"}],
                "harness": "claude-sdk",
            },
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "stream_not_supported_for_superside_chat"


@pytest.mark.asyncio
async def test_stream_true_is_allowed_for_a_plain_session() -> None:
    """A non-superside-chat session's ``?stream=true`` turn is unaffected."""
    server_client = _ThresholdServerClient(labels={}, items=_history_items())
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, _pm = _build_app(harness_client, server_client)
    conv_id = "conv_stream_plain"

    async with _runner_client(app) as client:
        resp = await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{conv_id}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "hello"}],
                    "harness": "claude-sdk",
                },
            ),
            timeout=5.0,
        )

    assert resp.status_code == 200, resp.text


# ── cold start reads the record from the checkpoint's anchor ─────────────


class _RecordServerClient(_HardLimitServerClient):
    """The items route in memory (``type``, ``order``, ``limit``, exclusive ``after``),
    recording every request made to it."""

    def __init__(self, *, labels: dict[str, str], items: list[dict[str, Any]]) -> None:
        super().__init__(labels=labels, items=items)
        self.item_requests: list[dict[str, str]] = []

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if not url.endswith("/items"):
            return await super().get(url, **kwargs)
        params = dict(kwargs.get("params") or {})
        self.item_requests.append(params)
        order = [it["id"] for it in self._items]
        rows = [it for it in self._items if params.get("type") in (None, it["type"])]
        desc = params.get("order") == "desc"
        if params.get("after"):
            assert params["after"] in order, "stale cursor"
            at = order.index(params["after"])
            rows = [
                it
                for it in rows
                if (order.index(it["id"]) < at if desc else order.index(it["id"]) > at)
            ]
        rows = rows[::-1] if desc else rows
        limit = int(params.get("limit", "100"))
        page, more = rows[:limit], len(rows) > limit

        class _Page(NullServerClient._Response):
            def json(self) -> dict[str, Any]:
                return {"data": page, "has_more": more}

        return _Page()


def _record_item(item_id: str, role: str, text: str) -> dict[str, Any]:
    kind = "input_text" if role == "user" else "output_text"
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "response_id": f"r_{item_id}",
        "created_at": int(time.time()),
        "role": role,
        "content": [{"type": kind, "text": text}],
    }


def _checkpoint_item(item_id: str, *, summary: str, last_item_id: str) -> dict[str, Any]:
    pair = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "HEADER"}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": summary}],
        },
    ]
    return {
        "id": item_id,
        "type": "compaction",
        "status": "completed",
        "response_id": f"compact_{item_id}",
        "created_at": int(time.time()),
        "summary": summary,
        "last_item_id": last_item_id,
        "model": "claude-test",
        "token_count": 10,
        "compacted_messages": pair,
    }


def _texts(content: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (m["role"], " ".join(b["text"] for b in m["content"] if "text" in b))
        for m in content
        if m.get("type") == "message"
    ]


async def _first_turn_content(
    server_client: _RecordServerClient, conv_id: str
) -> list[dict[str, Any]]:
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, _pm = _build_app(harness_client, server_client)
    async with _runner_client(app) as client:
        await asyncio.wait_for(_post_turn(client, conv_id, "hello"), timeout=5.0)
        await _wait_until(lambda: len(harness_client.posted_bodies) == 1, timeout=2.0)
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)
    return harness_client.posted_bodies[0]["content"]


@pytest.mark.asyncio
async def test_cold_start_reads_the_record_from_the_anchor_only() -> None:
    old = [
        _record_item(f"old{i}", "user" if i % 2 == 0 else "assistant", f"old {i}")
        for i in range(3000)
    ]
    during = [
        _record_item("d0", "user", "asked during"),
        _record_item("d1", "assistant", "answered"),
    ]
    after = [_record_item("n0", "user", "after"), _record_item("n1", "assistant", "after answer")]
    checkpoint = _checkpoint_item("c1", summary="REAL STATE", last_item_id="old2999")
    server_client = _RecordServerClient(
        labels=_SUPERSIDE_LABELS, items=[*old, *during, checkpoint, *after]
    )

    content = await _first_turn_content(server_client, "conv_cold_anchor")

    reads = [p for p in server_client.item_requests if p.get("order") == "asc"]
    assert reads == [{"limit": "1000", "order": "asc", "after": "old2999"}]
    # One assembled block (the working list once, the turn that ran while it summarized and
    # everything since), the last answer as it is, then this turn's message (ADR 0031).
    texts = _texts(content)
    block = texts[0][1]
    assert texts[0][0] == "user" and block.startswith(BLOCK_FRAME)
    for line in (
        "REAL STATE",
        "They said: asked during",
        "You said: answered",
        "They said: after",
    ):
        assert line in block
    assert "HEADER" not in block
    assert texts[1:-1] == [("assistant", "after answer")]
    assert "hello" in texts[-1][1]


@pytest.mark.asyncio
async def test_cold_start_without_a_checkpoint_reads_from_the_start() -> None:
    items = [_record_item("u1", "user", "question"), _record_item("a1", "assistant", "answer")]
    server_client = _RecordServerClient(labels=_SUPERSIDE_LABELS, items=items)

    content = await _first_turn_content(server_client, "conv_cold_plain")

    reads = [p for p in server_client.item_requests if p.get("order") == "asc"]
    assert reads == [{"limit": "1000", "order": "asc"}]
    texts = _texts(content)
    assert texts[0][1].startswith(BLOCK_FRAME) and "They said: question" in texts[0][1]
    assert texts[1] == ("assistant", "answer")


@pytest.mark.asyncio
async def test_cold_start_does_not_feed_a_placeholder_checkpoint_back_as_state() -> None:
    placeholder = "[Claude Code compaction — context was automatically compacted]"
    items = [
        _record_item("u1", "user", "old"),
        _record_item("a1", "assistant", "old answer"),
        _checkpoint_item("c1", summary="REAL STATE", last_item_id="a1"),
        _record_item("u2", "user", "work the placeholder covered"),
        _record_item("a2", "assistant", "its answer"),
        _checkpoint_item("c2", summary=placeholder, last_item_id="a2"),
    ]
    server_client = _RecordServerClient(labels=_SUPERSIDE_LABELS, items=items)

    content = await _first_turn_content(server_client, "conv_cold_placeholder")

    texts = _texts(content)
    assert "REAL STATE" in texts[0][1]
    assert "They said: work the placeholder covered" in texts[0][1]
    assert texts[1:-1] == [("assistant", "its answer")]
    assert not any(placeholder in text for _role, text in _texts(content))


# ── /compact in Super Chat mode is Omnigent's own compaction ─────────────


def _drain_events(app: Any, conv_id: str) -> list[str]:
    queue = app.state.session_event_queues.get(conv_id)
    types: list[str] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if event and str(event.get("type", "")).startswith("response.compaction"):
            types.append(event["type"])
    return types


@pytest.mark.asyncio
async def test_compact_in_superside_chat_mode_posts_a_real_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_client = _GatedLLMClient(asyncio.Event(), text="REAL STATE FILE")
    llm_client._gate.set()
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)
    server_client = _RecordServerClient(labels=_SUPERSIDE_LABELS, items=_history_items())
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, pm = _build_app(harness_client, server_client)
    conv_id = "conv_compact"

    async with _runner_client(app) as client:
        await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{conv_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "hello"}],
                    "harness_override": "claude-sdk",
                },
            ),
            timeout=5.0,
        )
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)
        _drain_events(app, conv_id)

        resp = await client.post(f"/v1/sessions/{conv_id}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text
        await _wait_until(lambda: len(server_client.posted_compactions) == 1, timeout=3.0)
        await _wait_until(lambda: pm.released == [conv_id], timeout=3.0)

    summary = server_client.posted_compactions[0]["data"]["summary"]
    assert "REAL STATE FILE" in summary
    assert "automatically compacted" not in summary
    # No literal /compact turn was sent to the SDK.
    assert len(harness_client.posted_bodies) == 1
    assert _drain_events(app, conv_id) == [
        "response.compaction.in_progress",
        "response.compaction.completed",
    ]


_STATE_TEXT = "## Working list\n- [t-launch · 2026-10-09] Spring launch budget"
_JOURNAL_REPLY = f"{_STATE_TEXT}\n<journal>\n2026-10-12 · Planned the launch\n</journal>"


class _RejectingCompactionClient(_RecordServerClient):
    """Refuses every compaction write with a 500."""

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        response = await super().post(url, **kwargs)
        body = kwargs.get("json", {})
        if isinstance(body, dict) and body.get("type") == "compaction":

            class _Failed(NullServerClient._Response):
                status_code = 500

            return _Failed()
        return response


async def _compact_once(
    monkeypatch: pytest.MonkeyPatch, server_client: _RecordServerClient, conv_id: str
) -> _FakeProcessManager:
    llm_client = _GatedLLMClient(asyncio.Event(), text=_JOURNAL_REPLY)
    llm_client._gate.set()
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)
    app, pm = _build_app(_SequencedHarnessClient([_plain_frames()]), server_client)
    async with _runner_client(app) as client:
        await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{conv_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "hello"}],
                    "harness_override": "claude-sdk",
                },
            ),
            timeout=5.0,
        )
        await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)
        resp = await client.post(f"/v1/sessions/{conv_id}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text
        await _wait_until(lambda: len(server_client.posted_compactions) == 1, timeout=3.0)
        await asyncio.sleep(0.2)
    return pm


@pytest.mark.asyncio
async def test_a_stored_checkpoint_writes_the_journal_and_state_files(
    monkeypatch: pytest.MonkeyPatch, _isolated_knowledge_workspace: Path
) -> None:
    server_client = _RecordServerClient(labels=_SUPERSIDE_LABELS, items=_history_items())

    await _compact_once(monkeypatch, server_client, "conv_journal")

    journal = (_isolated_knowledge_workspace / "memory" / "journal" / "2026-10-12.md").read_text()
    assert "- 2026-10-12 · Planned the launch · main <!-- conv_journal -->" in journal
    state = (_isolated_knowledge_workspace / "memory" / "STATE.md").read_text()
    assert "Spring launch budget" in state
    assert "Context checkpoint" not in state


@pytest.mark.asyncio
async def test_a_failed_checkpoint_post_writes_no_memory_files(
    monkeypatch: pytest.MonkeyPatch, _isolated_knowledge_workspace: Path
) -> None:
    server_client = _RejectingCompactionClient(labels=_SUPERSIDE_LABELS, items=_history_items())

    await _compact_once(monkeypatch, server_client, "conv_journal_failed")

    assert list((_isolated_knowledge_workspace / "memory").rglob("*.md")) == []


@pytest.mark.asyncio
async def test_a_turn_in_a_host_with_no_memory_folder_rebuilds_it(
    monkeypatch: pytest.MonkeyPatch, _isolated_knowledge_workspace: Path
) -> None:
    (_isolated_knowledge_workspace / "memory").rmdir()
    items = [
        *_history_items(),
        {
            **_checkpoint_item(
                "c1", summary=f"{checkpoint_header('size')}\n\n{_STATE_TEXT}", last_item_id="a1"
            ),
            "journal_lines": ["2026-10-11 · Asked for a budget"],
        },
    ]
    server_client = _RecordServerClient(labels=_SUPERSIDE_LABELS, items=items)
    app, _pm = _build_app(_SequencedHarnessClient([_plain_frames()]), server_client)

    async with _runner_client(app) as client:
        await asyncio.wait_for(_post_turn(client, "conv_lost_volume", "hello"), timeout=5.0)
        memory = _isolated_knowledge_workspace / "memory"
        await _wait_until(lambda: (memory / "STATE.md").exists(), timeout=3.0)

    journal = (memory / "journal" / "2026-10-11.md").read_text()
    assert "Asked for a budget · main <!-- conv_lost_volume -->" in journal


# ── return after idle: a cold start, not a rewrite ───────────────────────

_GAP_LINE = "It has been less than an hour since the last message."
# The idle period the tests set, and a pause just past it.
_IDLE_SECONDS = "1"
_PAST_IDLE = 1.2


def _last_user_text(body: dict[str, Any]) -> str:
    """The text of the last user message the harness was sent."""
    return [text for role, text in _texts(body["content"]) if role == "user"][-1]


@pytest.mark.asyncio
async def test_return_after_idle_keeps_the_warm_session_and_names_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS", _IDLE_SECONDS)
    llm_client = _GatedLLMClient(asyncio.Event())
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)
    server_client = _HardLimitServerClient(labels=_SUPERSIDE_LABELS, items=_history_items())
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, pm = _build_app(harness_client, server_client)
    conv_id = "conv_return_small"

    async with _runner_client(app) as client:
        await _turn(app, client, harness_client, conv_id)
        assert pm.released == []
        await asyncio.sleep(_PAST_IDLE)
        text = await _turn(app, client, harness_client, conv_id)

    assert llm_client.call_count == 0
    assert server_client.posted_compactions == []
    assert pm.released == []  # a return keeps the warm session (size is the only handoff)
    assert _GAP_LINE in text
    # The line is not persisted: it rides this turn's message only.
    assert _GAP_LINE not in str(server_client._items)


@pytest.mark.asyncio
async def test_a_turn_that_is_not_a_return_has_no_gap_line() -> None:
    server_client = _HardLimitServerClient(labels=_SUPERSIDE_LABELS, items=_history_items())
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, _pm = _build_app(harness_client, server_client)

    async with _runner_client(app) as client:
        await asyncio.wait_for(_post_turn(client, "conv_no_gap", "hello"), timeout=5.0)
        await _wait_until(lambda: len(harness_client.posted_bodies) == 1, timeout=2.0)

    assert "since the last message" not in _last_user_text(harness_client.posted_bodies[0])


@pytest.mark.asyncio
async def test_return_after_idle_with_more_than_the_tail_budget_never_summarizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A return never summarizes (ADR 0031): no summary is written or waited on."""
    monkeypatch.setenv("OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS", _IDLE_SECONDS)
    llm_client = _GatedLLMClient(asyncio.Event(), text="STATE")
    llm_client._gate.set()
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)
    server_client = _HardLimitServerClient(
        labels=_hard_limit_labels(), items=_long_history_items()
    )
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, _pm = _build_app(harness_client, server_client)
    conv_id = "conv_return_large"

    async with _runner_client(app) as client:
        await _turn(app, client, harness_client, conv_id)
        await asyncio.sleep(_PAST_IDLE)
        text = await _turn(app, client, harness_client, conv_id)

    assert llm_client.call_count == 0
    assert server_client.posted_compactions == []
    assert _GAP_LINE in text


# ── prefix delta: the profile and Projects go in full only when needed ───

_PROFILE = (
    "[Standing memory about the user, provided by the system, not a message from the "
    "user]\n\nPreferences:\n- Prefers figures in CAD\n\n[End of standing memory]"
)
_PROFILE_LINE = "Standing memory unchanged since the last message."


class _ProfileServerClient(_HardLimitServerClient):
    """Also serves the Memory Profile, which a test can change between turns."""

    def __init__(self, *, labels: dict[str, str], items: list[dict[str, Any]]) -> None:
        super().__init__(labels=labels, items=items)
        self.profile = _PROFILE

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url.endswith("/memory/profile"):
            profile = self.profile

            class _ProfileResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {"profile": profile}

            return _ProfileResponse()
        return await super().get(url, **kwargs)


async def _turn(
    app: Any,
    client: Any,
    harness_client: _SequencedHarnessClient,
    conv_id: str,
    *,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """Run one turn to completion and return the text of the message the harness got."""
    sent = len(harness_client.posted_bodies)
    body: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [{"type": "input_text", "text": f"question {sent}"}],
        "harness": "claude-sdk",
        "harness_override": "claude-sdk",
    }
    if tools is not None:
        body["tools"] = tools
    await asyncio.wait_for(client.post(f"/v1/sessions/{conv_id}/events", json=body), timeout=5.0)
    await _wait_until(lambda: len(harness_client.posted_bodies) == sent + 1, timeout=2.0)
    await _wait_until(lambda: conv_id not in app.state.active_turns, timeout=2.0)
    return _last_user_text(harness_client.posted_bodies[-1])


def _delta_app() -> tuple[Any, _FakeProcessManager, _SequencedHarnessClient, _ProfileServerClient]:
    server_client = _ProfileServerClient(labels=_SUPERSIDE_LABELS, items=_history_items())
    harness_client = _SequencedHarnessClient([_plain_frames()])
    app, pm = _build_app(harness_client, server_client)
    return app, pm, harness_client, server_client


@pytest.mark.asyncio
async def test_a_warm_client_gets_the_profile_once_then_one_line() -> None:
    app, _pm, harness_client, server_client = _delta_app()
    conv_id = "conv_delta_warm"

    async with _runner_client(app) as client:
        first = await _turn(app, client, harness_client, conv_id)
        second = await _turn(app, client, harness_client, conv_id)
        third = await _turn(app, client, harness_client, conv_id)
        server_client.profile = _PROFILE.replace("CAD", "EUR")
        changed = await _turn(app, client, harness_client, conv_id)
        after_change = await _turn(app, client, harness_client, conv_id)

    assert _PROFILE in first and _PROFILE_LINE not in first
    for text in (second, third, after_change):
        assert _PROFILE_LINE in text and "Prefers figures" not in text
    assert "Prefers figures in EUR" in changed and _PROFILE_LINE not in changed


@pytest.mark.asyncio
async def test_warm_turns_with_an_unchanged_profile_add_under_300_prefix_tokens() -> None:
    from omnigent.context.rollover import count_tokens
    from omnigent.models.model_fallbacks import ROLLOVER_TOKEN_COUNT_MODEL

    app, _pm, harness_client, _server = _delta_app()
    conv_id = "conv_delta_budget"
    prefix_tokens: list[int] = []

    async with _runner_client(app) as client:
        for _ in range(50):
            text = await _turn(app, client, harness_client, conv_id)
            prefix = text.removesuffix(f"question {len(harness_client.posted_bodies) - 1}")
            prefix_tokens.append(
                count_tokens(
                    [{"type": "message", "role": "user", "content": prefix}],
                    ROLLOVER_TOKEN_COUNT_MODEL,
                )
            )

    assert prefix_tokens[0] > 0
    assert max(prefix_tokens[1:]) < 300


async def _drop_via_compact(app: Any, client: Any, conv_id: str, pm: _FakeProcessManager) -> None:
    resp = await client.post(f"/v1/sessions/{conv_id}/events", json={"type": "compact"})
    assert resp.status_code == 200, resp.text
    await _wait_until(lambda: pm.released == [conv_id], timeout=3.0)
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_after_a_warm_client_drop_the_next_turn_carries_the_profile_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_client = _GatedLLMClient(asyncio.Event(), text="STATE")
    llm_client._gate.set()
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)
    app, pm, harness_client, _server = _delta_app()
    conv_id = "conv_delta_drops"

    async with _runner_client(app) as client:
        await _turn(app, client, harness_client, conv_id)
        assert _PROFILE_LINE in await _turn(app, client, harness_client, conv_id)

        # A rollover (here a manual /compact) drops the client.
        await _drop_via_compact(app, client, conv_id, pm)
        assert _PROFILE in await _turn(app, client, harness_client, conv_id)
        assert _PROFILE_LINE in await _turn(app, client, harness_client, conv_id)

        # The harness process was released or restarted behind the runner's back.
        await pm.release(conv_id)
        assert _PROFILE in await _turn(app, client, harness_client, conv_id)
        assert _PROFILE_LINE in await _turn(app, client, harness_client, conv_id)

        # A turn that failed leaves nothing remembered: the client may be gone.
        failed = _SequencedHarnessClient([_failed_frames(), _plain_frames()])
        harness_client._sequences = failed._sequences
        harness_client.call_count = 0
        await _turn(app, client, harness_client, conv_id)
        assert _PROFILE in await _turn(app, client, harness_client, conv_id)
        assert _PROFILE_LINE in await _turn(app, client, harness_client, conv_id)


def _failed_frames() -> list[str]:
    return [
        _sse({"type": "response.created", "response": {"id": "resp_failed"}}),
        _sse({"type": "response.failed", "error": {"message": "boom", "type": "server_error"}}),
    ]


@pytest.mark.asyncio
async def test_the_hard_limit_drops_the_remembered_profile_and_a_return_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS", _IDLE_SECONDS)
    llm_client = _GatedLLMClient(asyncio.Event(), text="STATE")
    llm_client._gate.set()
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: llm_client)
    app, _pm, harness_client, server_client = _delta_app()
    conv_id = "conv_delta_idle"

    async with _runner_client(app) as client:
        await _turn(app, client, harness_client, conv_id)
        assert _PROFILE_LINE in await _turn(app, client, harness_client, conv_id)

        await asyncio.sleep(_PAST_IDLE)
        back = await _turn(app, client, harness_client, conv_id)
        # A return keeps the warm session, so the profile it already has is one line.
        assert _PROFILE_LINE in back and _GAP_LINE in back
        assert _PROFILE_LINE in await _turn(app, client, harness_client, conv_id)

        # Past the hard limit the turn cold-starts from the record.
        server_client._labels = _hard_limit_labels()
        harness_client._sequences = [_triggering_frames(500_000), _plain_frames()]
        harness_client.call_count = 0
        await _turn(app, client, harness_client, conv_id)
        assert _PROFILE in await _turn(app, client, harness_client, conv_id)


@pytest.mark.asyncio
async def test_a_changed_system_prompt_makes_the_next_turn_carry_the_profile_again() -> None:
    """The executor rebuilds its client when the prompt it builds changes (a new tool set
    here), and replays the persisted record, which holds no prefix blocks."""
    app, _pm, harness_client, _server = _delta_app()
    conv_id = "conv_delta_prompt"
    tool = {
        "type": "function",
        "function": {"name": "client_tool", "parameters": {"type": "object"}},
    }

    async with _runner_client(app) as client:
        await _turn(app, client, harness_client, conv_id)
        assert _PROFILE_LINE in await _turn(app, client, harness_client, conv_id)
        assert _PROFILE in await _turn(app, client, harness_client, conv_id, tools=[tool])
        assert _PROFILE_LINE in await _turn(app, client, harness_client, conv_id, tools=[tool])
