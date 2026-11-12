"""A message sent to a session: a turn when idle, else the running turn's (steered in by the
harness's own steer, or run right after it). The runner alone owns the waiting messages.

The POST answers with the runner's ``turn`` number the message started or joined; the
session's ``idle`` edge carries it and comes only once nothing waits. ``session.input.delivery``
events are display hints for a message that waited.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.runner.conftest import _runner_client, _sse
from tests.runner.test_app_sessions_native_workflow_messages import (
    _build_handshake_app,
    _HandshakeHarnessClient,
)

SESSION = "ede98a0180773a70b1e81cc854ff7d8a"
AGENT = "880b5afda28ad55ff74cbeb9b5fc67fb"


def _message(text: str, item_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [{"type": "input_text", "text": text}],
        "harness": "claude-sdk",
    }
    if item_id:
        body["persisted_item_id"] = item_id
    return body


def _events(app: Any, queue: Any = None) -> list[dict[str, Any]]:
    queue = queue or app.state.session_event_queues.get(SESSION)
    events: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if event:
            events.append(event)
    return events


def _pings(app: Any, queue: Any = None) -> list[str]:
    """Item ids of the queue-changed pings (``session.input.delivery``), in order."""
    return [
        e["data"]["item_id"] for e in _events(app, queue) if e["type"] == "session.input.delivery"
    ]


class _RefusingHarness(_HandshakeHarnessClient):
    """A harness that cannot take a message into the running turn."""

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        self.posted_bodies.append(json)
        gate, injections = self._gate, self._consumed_ids
        is_first = len(self.posted_bodies) == 1

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> Any:
                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                yield _sse({"type": "response.created", "response": {"id": "resp_1"}})
                if is_first:
                    await gate.wait()
                    for inj_id in list(injections):
                        yield _sse({"type": "injection.refused", "injection_id": inj_id})
                yield _sse({"type": "response.completed", "response": {"id": "resp_1"}})

        return _Ctx()


async def _send_during_a_turn(app: Any, harness: Any, gate: asyncio.Event) -> dict[str, Any]:
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        first = await client.post(f"/v1/sessions/{SESSION}/events", json=_message("redo", "i1"))
        assert first.status_code == 202
        await asyncio.sleep(0.05)
        second = await client.post(
            f"/v1/sessions/{SESSION}/events", json=_message("just redo the deck again", "i2")
        )
        assert second.json()["status"] == "buffered"
        # Joins the running turn: same number as the turn the first message started.
        assert second.json()["turn"] == first.json()["turn"]
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(harness.posted_bodies) >= 2:
                break
        await asyncio.sleep(0.05)
        return first.json()


async def _gate_when_forwarded(harness: Any, gate: asyncio.Event) -> None:
    """Let the turn go on once the runner has forwarded the injection to the harness."""
    while not harness.patched_events:
        await asyncio.sleep(0.005)
    gate.set()


@pytest.mark.asyncio
async def test_a_message_the_harness_takes_into_the_turn_is_steered_once() -> None:
    gate = asyncio.Event()
    app, _pm, harness = _build_handshake_app(gate)

    await _send_during_a_turn(app, harness, gate)

    assert _pings(app) == []  # offered and read: it never waited
    assert len(harness.posted_bodies) == 1  # one turn, not two


@pytest.mark.asyncio
async def test_a_message_the_harness_refuses_runs_as_the_next_turn() -> None:
    gate = asyncio.Event()
    app, pm, _ = _build_handshake_app(gate)
    harness = _RefusingHarness(gate)
    pm._client = harness  # type: ignore[attr-defined]

    await _send_during_a_turn(app, harness, gate)

    assert _pings(app) == ["i2", "i2"]  # started waiting, then left the wait as a turn
    assert len(harness.posted_bodies) == 2


@pytest.mark.asyncio
async def test_idle_comes_only_once_nothing_waits_and_names_the_last_turn() -> None:
    gate = asyncio.Event()
    app, pm, _ = _build_handshake_app(gate)
    harness = _RefusingHarness(gate)
    pm._client = harness  # type: ignore[attr-defined]

    first = await _send_during_a_turn(app, harness, gate)

    statuses = [
        (e["status"], e.get("turn")) for e in _events(app) if e["type"] == "session.status"
    ]
    # No idle between the first turn and the queued one it ran right after.
    assert [status for status, _ in statuses] == ["running", "running", "idle"]
    assert statuses[-1][1] > first["turn"]  # the idle closes the later turn


@pytest.mark.asyncio
async def test_a_message_to_an_idle_session_starts_a_numbered_turn() -> None:
    gate = asyncio.Event()
    app, _pm, _harness = _build_handshake_app(gate)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        answer = await client.post(f"/v1/sessions/{SESSION}/events", json=_message("hi", "i1"))
        gate.set()
        await asyncio.sleep(0.05)
    assert (answer.status_code, answer.json()["status"]) == (202, "accepted")
    running = next(e for e in _events(app) if e["type"] == "session.status")
    assert running["turn"] == answer.json()["turn"]


@pytest.mark.asyncio
async def test_a_message_only_for_a_running_turn_is_refused_by_an_idle_session() -> None:
    gate = asyncio.Event()
    app, _pm, harness = _build_handshake_app(gate)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        body = {**_message("a note for the running turn", "i1"), "if_running": True}
        answer = await client.post(f"/v1/sessions/{SESSION}/events", json=body)
    assert (answer.status_code, answer.json()["error"]) == (409, "not_running")
    assert harness.posted_bodies == []  # no turn was started for it


@pytest.mark.asyncio
async def test_a_message_only_for_a_running_turn_steers_into_one() -> None:
    gate = asyncio.Event()
    app, _pm, harness = _build_handshake_app(gate)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("work", "i1"))
        await asyncio.sleep(0.05)
        body = {**_message("a note", "i2"), "if_running": True}
        release = asyncio.create_task(_gate_when_forwarded(harness, gate))
        # Answered once the running turn has its verdict on the note, not before.
        answer = await client.post(f"/v1/sessions/{SESSION}/events", json=body)
        await release
        await asyncio.sleep(0.1)
    assert answer.status_code == 202
    assert "if_running" not in harness.patched_events[-1]
    assert not any(k.startswith("_only") for k in harness.patched_events[-1])
    assert _pings(app) == []


@pytest.mark.asyncio
async def test_a_message_only_for_a_running_turn_that_refuses_it_never_runs() -> None:
    gate = asyncio.Event()
    app, pm, _ = _build_handshake_app(gate)
    harness = _RefusingHarness(gate)
    pm._client = harness  # type: ignore[attr-defined]
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("work", "i1"))
        await asyncio.sleep(0.05)
        body = {**_message("a note", "i2"), "if_running": True}
        release = asyncio.create_task(_gate_when_forwarded(harness, gate))
        answer = await client.post(f"/v1/sessions/{SESSION}/events", json=body)
        await release
        await asyncio.sleep(0.1)
    assert (answer.status_code, answer.json()["error"]) == (409, "not_running")
    assert len(harness.posted_bodies) == 1  # it did not become a turn of its own
    assert not app.state.session_message_buffers.get(SESSION)
    assert _pings(app) == []  # the server removes the item; it never happened


@pytest.mark.asyncio
async def test_a_message_only_for_a_running_turn_is_refused_when_it_cannot_be_offered() -> None:
    gate = asyncio.Event()
    app, _pm, harness = _build_handshake_app(gate)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("work", "i1"))
        await asyncio.sleep(0.05)
        body = {
            **_message("look", "i2"),
            "content": [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}],
            "if_running": True,
        }
        answer = await client.post(f"/v1/sessions/{SESSION}/events", json=body)
        gate.set()
        await asyncio.sleep(0.1)
    assert (answer.status_code, answer.json()["error"]) == (409, "not_running")
    assert harness.patched_events == []
    assert _pings(app) == []


@pytest.mark.asyncio
async def test_a_message_with_an_image_is_not_steered_but_runs_whole_as_a_turn() -> None:
    gate = asyncio.Event()
    app, _pm, harness = _build_handshake_app(gate)
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("work", "i1"))
        await asyncio.sleep(0.05)
        image = [
            {"type": "input_text", "text": "what is this?"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
        ]
        answer = await client.post(
            f"/v1/sessions/{SESSION}/events", json={**_message("x", "i2"), "content": image}
        )
        waiting = await client.get(f"/v1/sessions/{SESSION}/buffered")
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(harness.posted_bodies) >= 2:
                break
    assert answer.json()["status"] == "buffered"
    assert waiting.json() == {"item_ids": ["i2"]}  # the live queue the transcript reads
    assert harness.patched_events == []  # never offered in part
    assert _pings(app) == ["i2", "i2"]


@pytest.mark.asyncio
async def test_a_stop_cancels_the_messages_waiting_behind_the_turn() -> None:
    gate = asyncio.Event()
    app, pm, _ = _build_handshake_app(gate)
    harness = _RefusingHarness(gate)
    pm._client = harness  # type: ignore[attr-defined]
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("work", "i1"))
        await asyncio.sleep(0.05)
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("and this", "i2"))
        await client.post(f"/v1/sessions/{SESSION}/events", json={"type": "interrupt"})
        gate.set()
        await asyncio.sleep(0.1)
    assert "i2" in _pings(app)
    assert not app.state.session_message_buffers.get(SESSION)
    assert len(harness.posted_bodies) == 1  # it did not run after the stop


@pytest.mark.asyncio
async def test_deleting_the_session_cancels_the_messages_still_waiting() -> None:
    gate = asyncio.Event()
    app, pm, _ = _build_handshake_app(gate)
    harness = _RefusingHarness(gate)
    pm._client = harness  # type: ignore[attr-defined]
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("work", "i1"))
        await asyncio.sleep(0.05)
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("and this", "i2"))
        stream = app.state.session_event_queues[SESSION]  # closed and dropped by the delete
        await client.delete(f"/v1/sessions/{SESSION}")
    assert _pings(app, stream) == ["i2"]


@pytest.mark.asyncio
async def test_a_message_attaching_a_file_runs_as_its_own_turn_not_merged() -> None:
    """A file reference needs its turn-start context (the framed file text the host puts in
    front of the turn's own message), so it is never steered and never merged with others."""
    gate = asyncio.Event()
    app, _pm, harness = _build_handshake_app(gate)
    attached = (
        "Attached file in your workspace: your_files/uploads/d/report.pdf "
        "(application/pdf, 10 bytes)\n\nsummarise it"
    )
    async with _runner_client(app) as client:
        await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("work", "i1"))
        await asyncio.sleep(0.05)
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message(attached, "i2"))
        await client.post(f"/v1/sessions/{SESSION}/events", json=_message("and blue", "i3"))
        gate.set()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(harness.posted_bodies) >= 3:
                break
    # i2 never reached the running turn; i3 was steered into it (plain text).
    assert [e.get("persisted_item_id") for e in harness.patched_events] == ["i3"]
    assert len(harness.posted_bodies) == 2  # the first turn, then i2 alone as its own
    last_turn = harness.posted_bodies[-1]["content"][-1]
    assert last_turn["content"][0]["text"] == attached  # whole, as its own turn's message
    assert "i2" in _pings(app)


class _SilentHarness(_HandshakeHarnessClient):
    """A harness that never answers an injection (no consumed, no refused)."""

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        self.patched_events.append(json)

        class _Response:
            status_code = 204
            headers: dict[str, str] = {}
            content = b""

        return _Response()


@pytest.mark.asyncio
async def test_a_helper_turn_ending_with_a_note_unanswered_still_delivers_its_result() -> None:
    """A note meant only for the Helper's running turn never holds back that turn's end: the
    sender hears ``not_running`` and the parent gets the Helper's result."""
    from omnigent.runner import app as runner_app

    gate = asyncio.Event()
    app, pm, _ = _build_handshake_app(gate)
    harness = _SilentHarness(gate)
    pm._client = harness  # type: ignore[attr-defined]
    runner_app.register_subagent_work(
        parent_session_id="conv_parent", child_session_id=SESSION, agent="worker", title="Deck"
    ).status = "running"
    try:
        async with _runner_client(app) as client:
            await client.post("/v1/sessions", json={"session_id": SESSION, "agent_id": AGENT})
            await client.post(f"/v1/sessions/{SESSION}/events", json=_message("work", "i1"))
            await asyncio.sleep(0.05)
            note = asyncio.create_task(
                client.post(
                    f"/v1/sessions/{SESSION}/events",
                    json={**_message("a note", "i2"), "if_running": True},
                )
            )
            while not harness.patched_events:
                await asyncio.sleep(0.005)
            gate.set()  # the Helper's turn ends before it answers the note
            answer = await asyncio.wait_for(note, 5)
            await asyncio.sleep(0.1)
        entry = runner_app.get_subagent_work(SESSION)
    finally:
        runner_app.unregister_subagent_work(SESSION)
    assert (answer.status_code, answer.json()["error"]) == (409, "not_running")
    assert entry is not None and entry.status == "completed"  # the result goes to the parent
    statuses = [e["status"] for e in _events(app) if e["type"] == "session.status"]
    assert statuses[-1] in ("idle", "waiting")
