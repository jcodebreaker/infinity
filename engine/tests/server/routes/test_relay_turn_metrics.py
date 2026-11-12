"""The relay's per-turn wait lines (ADR 0026), measured from the person's message."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.routes._sessions.turn_metrics import TurnClock
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_LOGGER = "omnigent.server.routes._sessions.turn_metrics"
_TOOL = {
    "type": "response.output_item.done",
    "item": {
        "type": "function_call",
        "name": "deck_new",
        "call_id": "c1",
        "status": "in_progress",
    },
}
_DELTA = {"type": "response.output_text.delta", "delta": "Making the deck."}
_DONE = {"type": "response.completed", "response": {"id": "resp_1"}}


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER]


def test_a_spoken_turn_logs_first_visible_event_and_first_word_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = TurnClock("conv_1")
    clock.arm(("msg_1", 100))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        clock.observe({"type": "response.in_progress"}, now=100.5)
        clock.observe(_DELTA, now=101.0)
        clock.observe(_DELTA, now=101.2)
        clock.observe(_TOOL, now=102.0)
        clock.observe(_DONE, now=150.0)
    assert _lines(caplog) == [
        "first visible event for conv_1 1.0s after the message",
        "first word for conv_1 1.0s after the message",
    ]
    record = next(r for r in caplog.records if r.name == _LOGGER)
    assert record.__dict__["session_id"] == "conv_1"


def test_a_silent_turn_logs_the_tool_call_and_no_text(caplog: pytest.LogCaptureFixture) -> None:
    clock = TurnClock("conv_1")
    clock.arm(("msg_1", 100))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        clock.observe(_TOOL, now=103.0)
        clock.observe(_DONE, now=282.0)
        # A late frame after the end logs nothing more.
        clock.observe(_DELTA, now=283.0)
    assert _lines(caplog) == [
        "first visible event for conv_1 3.0s after the message",
        "no text for conv_1: turn ended after 182.0s without a word",
    ]


def test_a_used_or_missing_message_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    clock = TurnClock("conv_1")
    clock.arm(("msg_1", 100))
    clock.observe(_DONE, now=101.0)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        # A second response for the same message (a Helper's notice wakes the turn).
        clock.arm(("msg_1", 100))
        clock.observe(_DELTA, now=300.0)
        clock.observe(_DONE, now=301.0)
        clock.arm(None)
        clock.observe(_DONE, now=302.0)
        # A malformed frame never raises.
        clock.observe({"type": "response.output_item.done", "item": "junk"}, now=303.0)
    assert _lines(caplog) == []


def test_a_second_response_mid_turn_keeps_the_clock(caplog: pytest.LogCaptureFixture) -> None:
    clock = TurnClock("conv_1")
    clock.arm(("msg_1", 100))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        clock.observe(_TOOL, now=101.0)
        clock.arm(("msg_1", 100))
        clock.observe(_DELTA, now=105.0)
    assert _lines(caplog)[-1] == "first word for conv_1 5.0s after the message"


def test_the_acceptance_stamp_wins_over_the_whole_second_created_at(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = TurnClock("conv_1")
    # Accepted at 100.9 (monotonic 5000.0); created_at was truncated to 100.
    clock.arm(("msg_1", 100), accepted=(100.9, 5000.0))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        clock.observe(_DELTA, now=5000.4)
    assert _lines(caplog)[0] == "first visible event for conv_1 0.4s after the message"
    # A stamp from another message (far from created_at) is ignored.
    other = TurnClock("conv_2")
    other.arm(("msg_2", 100), accepted=(50.0, 10.0))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        other.observe(_DELTA, now=101.5)
    assert _lines(caplog)[0] == "first visible event for conv_2 1.5s after the message"


def test_mark_accepted_stamps_the_session() -> None:
    from omnigent.server.routes._sessions import turn_metrics

    turn_metrics.mark_accepted("conv_stamp")
    wall, _ = turn_metrics._accepted["conv_stamp"]
    assert abs(wall - time.time()) < 5


def test_a_native_message_item_counts_as_the_first_word(caplog: pytest.LogCaptureFixture) -> None:
    clock = TurnClock("conv_1")
    clock.arm(("msg_1", 100))
    item = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Hi"}],
    }
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        clock.observe({"type": "response.output_item.done", "item": item}, now=100.4)
        clock.observe(_DONE, now=101.0)
    assert _lines(caplog) == [
        "first visible event for conv_1 0.4s after the message",
        "first word for conv_1 0.4s after the message",
    ]


class _Stream:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        """The scripted stream is a successful response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        for event in self._events:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"


class _Runner:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def stream(self, method: str, path: str, *, timeout: Any) -> _Stream:
        del method, path, timeout
        return _Stream(self._events)


@pytest.mark.asyncio
async def test_the_relay_times_a_silent_turn_from_the_persons_message(
    db_uri: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The relay arms the clock from the stored message and logs the silent turn."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once

    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                status="completed",
                response_id="resp_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "create some presentation"}],
                ),
            )
        ],
    )
    events = [
        {"type": "response.in_progress", "response": {"id": "resp_1", "model": "m"}},
        _TOOL,
        _DONE,
    ]
    try:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await asyncio.wait_for(
                _relay_runner_stream_once(session_id, _Runner(events), store),  # type: ignore[arg-type]
                timeout=10,
            )
    finally:
        session_stream.close(session_id)
    lines = _lines(caplog)
    assert [line.split(" for ")[0] for line in lines] == ["first visible event", "no text"]
    assert all(session_id in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "named"),
    [({"omnigent.context.mode": "superside-chat"}, True), ({}, False)],
)
async def test_a_running_super_chat_turn_names_its_activity(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, labels: dict[str, str], named: bool
) -> None:
    """While a Super Chat turn runs its status edges carry ``activity_id``, the turn's Activity
    id; the idle edge after it, and any other session's edges, carry none."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once

    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation(labels=labels).id
    published: list[dict[str, Any]] = []
    real_publish = session_stream.publish

    def capture(sid: str, payload: dict[str, Any]) -> None:
        if payload.get("type") == "session.status":
            published.append(payload)
        real_publish(sid, payload)

    monkeypatch.setattr(session_stream, "publish", capture)
    events = [
        {"type": "session.status", "status": "running"},
        {"type": "response.in_progress", "response": {"id": "resp_1", "model": "m"}},
        {"type": "session.status", "status": "running", "blocked_on": "permission prompt"},
        _DONE,
        {"type": "session.status", "status": "idle"},
    ]
    try:
        await asyncio.wait_for(
            _relay_runner_stream_once(session_id, _Runner(events), store),  # type: ignore[arg-type]
            timeout=10,
        )
    finally:
        session_stream.close(session_id)
    activity = f"turn:{session_id}:resp_1"
    seen = [(p["status"], p.get("activity_id")) for p in published]
    if named:
        assert seen == [
            ("running", None),
            ("running", activity),
            ("running", activity),
            ("idle", None),
        ]
    else:
        assert all(a is None for _, a in seen)
