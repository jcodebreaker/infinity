"""Tests for subagent status supervision, delivery, and parent wakeups."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _build_interrupt_app,
    _FakeProcessManager,
    _interrupt_markers,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_interrupt_inserts_cancellation_items_in_history() -> None:
    """Interrupting a turn with dangling function_calls inserts synthetic outputs.

    When the user interrupts mid-tool-chain, any ``function_call``
    items that were emitted by the harness but never received a
    ``function_call_output`` must get synthetic cancelled outputs.
    A cancellation marker message must also be appended so the LLM
    knows the prior turn was incomplete. This matches the DBOS
    path's ``_append_cancellation_item`` behavior.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "85b147537400967b1fb8542367423306"
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_int_test"},
        )
        assert create_resp.status_code == 201, create_resp.text

        # Start the turn — it blocks after the first function_call
        # frame, before the second one and response.completed.
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "claude-sdk",
            },
        )
        assert resp.status_code == 202
        # Let the background turn reach the gate.
        await _aio.sleep(0.1)

        # Send an interrupt while the turn is blocked.
        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        # The harness stub returns 200; the real scaffold returns
        # 204. Both are success — we care about the side-effect
        # (cancellation items), not the status code.
        assert int_resp.status_code in (200, 204), (
            f"Interrupt must succeed; got {int_resp.status_code}"
        )

        # Release the gate so the harness stream finishes and
        # _on_proxy_stream_end fires with the interrupt flag.
        gate.set()
        await _aio.sleep(0.2)

    # Access the runner's in-memory history for this session.
    histories = _session_histories_ref.get(conv_id, [])

    # The history should contain synthetic function_call_output
    # items for each dangling call_id. call_a was emitted before
    # the gate (always present). call_b may or may not have been
    # emitted depending on timing, but at minimum call_a should
    # have a synthetic output.
    synthetic_outputs = [
        h
        for h in histories
        if h.get("type") == "function_call_output"
        and h.get("output") == "[Cancelled — tool execution was interrupted.]"
    ]
    dangling_calls = [h for h in histories if h.get("type") == "function_call"]
    matched_real_outputs = [
        h
        for h in histories
        if h.get("type") == "function_call_output"
        and h.get("output") != "[Cancelled — tool execution was interrupted.]"
    ]
    dangling_call_ids = {c["call_id"] for c in dangling_calls}
    real_output_call_ids = {o["call_id"] for o in matched_real_outputs}
    orphan_ids = dangling_call_ids - real_output_call_ids
    synthetic_output_call_ids = {o["call_id"] for o in synthetic_outputs}

    # Every orphaned function_call must have a synthetic output.
    # If empty, _append_cancellation_items didn't fire (the interrupt
    # flag wasn't set or wasn't checked in _on_proxy_stream_end).
    assert orphan_ids == synthetic_output_call_ids, (
        f"Every dangling function_call must get a synthetic cancelled "
        f"output. Orphan call_ids={orphan_ids}, synthetic output "
        f"call_ids={synthetic_output_call_ids}. If synthetic is empty, "
        f"_append_cancellation_items was not called on interrupt."
    )

    # Cancellation marker message must be present so the LLM knows
    # the prior output was truncated. If missing, the next turn's
    # context is silently incomplete.
    markers = [
        h
        for h in histories
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert len(markers) == 1, (
        f"Expected exactly 1 cancellation marker message, "
        f"got {len(markers)}. If 0, _append_cancellation_items "
        f"didn't insert the marker."
    )


@pytest.mark.asyncio
async def test_interrupt_cancel_floor_finalizes_stuck_turn() -> None:
    """The cancel floor: interrupt force-cancels a turn the harness never finishes.

    Sister to ``test_interrupt_inserts_cancellation_items_in_history``, but the
    gate is NEVER released — the harness stream stays blocked forever, and the
    forwarded interrupt (recorded by the stub but ignored) does not unblock it.
    The turn can therefore only end because the runner force-cancels its turn
    task (``_cancel_active_turn``). If that floor regresses to forward-only, the
    turn stays stuck and no cancellation marker is ever appended.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()  # never set — only the floor's task-cancel can end the turn
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "97d2b96d733e685433e2b3864eb97652"
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_int_test"},
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "claude-sdk",
            },
        )
        assert resp.status_code == 202
        # Deterministic: the harness stream sets post_seen when it begins, so
        # the turn is in flight and blocked on the gate before we interrupt.
        await _aio.wait_for(_hc.post_seen.wait(), timeout=5.0)

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        # The handler awaits the cancel, so the turn is finalized by the time
        # this returns — capture history before the client context tears down.
        assert int_resp.status_code == 204, int_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

    assert len(markers) == 1, (
        f"The cancel floor must finalize a stuck turn even though the gate was "
        f"never released; got {len(markers)} interrupted markers. 0 means the "
        f"turn is still blocked — interrupt only forwarded to the harness "
        f"without cancelling the runner turn task."
    )


@pytest.mark.asyncio
async def test_stop_session_cancels_inprocess_turn() -> None:
    """``stop_session`` cancels an in-process harness's in-flight turn.

    For non-native harnesses this used to be a 204 no-op — the sidebar Stop did
    nothing. It now routes through the same cancel floor as interrupt: with the
    gate never released, the blocked turn ends only because stop_session
    force-cancels the turn task. 0 markers means the no-op regressed.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()  # never set
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "422963919abf3c166633d99ea20f2b8e"
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_int_test"},
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "claude-sdk",
            },
        )
        assert resp.status_code == 202
        # Deterministic: wait for the harness stream to begin (turn in flight,
        # blocked on the gate) before stopping.
        await _aio.wait_for(_hc.post_seen.wait(), timeout=5.0)

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "stop_session"},
        )
        assert stop_resp.status_code == 204, stop_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

    assert len(markers) == 1, (
        f"stop_session must cancel the in-flight in-process turn (was a 204 "
        f"no-op); got {len(markers)} interrupted markers. 0 means stop_session "
        f"still no-ops for non-native harnesses."
    )


@pytest.mark.asyncio
async def test_interrupt_during_setup_phase_recovers_stuck_turn() -> None:
    """Interrupt during the setup phase finalizes the turn — the session isn't stuck.

    A cancel that lands while the turn is still in setup (here: blocked in the
    background turn's spec resolution, before ``_drain_streaming_response`` is
    entered) raises ``CancelledError`` past ``_run_turn_bg``'s
    ``except Exception`` (it's a ``BaseException``), so neither the drain handler
    nor the setup handler cleans up. Without the floor's setup-phase recovery,
    ``_active_turns`` keeps the done task — every later message buffers behind a
    turn that never runs and the session hangs.

    Proof: after the setup-phase interrupt, a NEW message must start a fresh
    turn (its setup re-enters the resolver). With the bug it would be buffered
    behind the stale ``_active_turns`` entry and never run. Also asserts exactly
    one interrupted marker from the cancelled turn.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    resolver_gate = _aio.Event()  # released only in teardown → spec resolution blocks
    resolver_entered = _aio.Event()
    spec = AgentSpec(spec_version=1, name="t")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Signal entry, then block so the background turn stalls in setup."""
        del agent_id, session_id
        resolver_entered.set()
        await resolver_gate.wait()
        return spec

    # Frames let the eventual (post-teardown) turn drain cleanly.
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_s"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_s"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    conv_id = "9fb432c546c7dbf9f34c6acbc861b05f"
    # agent_id is required for the background turn's setup to invoke the resolver.
    msg = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
        "content": [{"type": "input_text", "text": "do something"}],
        "harness": "claude-sdk",
    }
    fresh_turn_started = False
    async with _runner_client(app) as client:
        # First turn: the handler returns 202, then the background turn blocks in
        # spec resolution (the setup phase).
        r1 = await client.post(f"/v1/sessions/{conv_id}/events", json=msg)
        assert r1.status_code == 202
        await _aio.wait_for(resolver_entered.wait(), timeout=5.0)

        # Interrupt while the turn is still in setup.
        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        assert int_resp.status_code == 204, int_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

        # The session must accept a new turn. If _active_turns were left stale,
        # this message buffers behind the dead turn and never starts a new one,
        # so the resolver is never re-entered.
        resolver_entered.clear()
        r2 = await client.post(f"/v1/sessions/{conv_id}/events", json=msg)
        assert r2.status_code == 202
        try:
            await _aio.wait_for(resolver_entered.wait(), timeout=5.0)
            fresh_turn_started = True
        except _aio.TimeoutError:
            fresh_turn_started = False

        resolver_gate.set()  # release the blocked turn for clean teardown
        await _aio.sleep(0.1)

    assert fresh_turn_started, (
        "After a setup-phase interrupt the session must accept a new turn; the "
        "follow-up message never re-entered spec resolution, so _active_turns was "
        "left stale and the session is stuck (the bug this guards)."
    )
    # Exactly one marker = only the interrupted first turn was finalized.
    assert len(markers) == 1, (
        f"The interrupted setup-phase turn must append exactly one marker; got {len(markers)}."
    )


@pytest.mark.asyncio
async def test_interrupt_marker_instructs_model_to_disregard_abandoned_request() -> None:
    """The cancellation marker tells the model to drop the canceled request.

    The bug this guards against: a marker that only says the assistant
    reply was cut off leaves the canceled user instruction in history,
    so the next turn replays it and the agent follows the abandoned
    request. The marker must explicitly instruct the model not to resume
    the interrupted request and to treat the next user message as current.
    A revert to the old "halted the agent response / may be incomplete"
    text (no disregard instruction) turns this test red.

    The server's Stop line (ADR 0029) rides on the interrupt and is written into
    that same marker; a Stop with no turn running writes the line alone, so the
    next turn reads what was stopped either way.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "86ba5bc9053ae8771fdb988e882f04b5"
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_int_test"},
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "delete all my files"}],
                "harness": "claude-sdk",
            },
        )
        assert resp.status_code == 202
        await _aio.sleep(0.1)

        line = "Stopped. Nothing was saved."
        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt", "data": {"note": line}},
        )
        assert int_resp.status_code in (200, 204)

        gate.set()
        await _aio.sleep(0.2)

        idle_line = "Stopped. 'Uruguay deck' was still working; nothing was saved."
        idle_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt", "data": {"note": idle_line}},
        )
        assert idle_resp.status_code in (200, 204)

    histories = _session_histories_ref.get(conv_id, [])
    texts = [
        b.get("text") or ""
        for h in histories
        if h.get("type") == "message" and h.get("role") == "user"
        for b in h.get("content", [])
    ]
    assert texts[-1] == f"[System: stopped]\n{idle_line}"
    marker_texts = [
        b.get("text") or ""
        for h in histories
        if h.get("type") == "message" and h.get("role") == "user"
        for b in h.get("content", [])
        if "interrupted" in (b.get("text") or "").lower()
    ]
    assert len(marker_texts) == 1, (
        f"Expected exactly 1 interrupted marker, got {len(marker_texts)}."
    )
    marker = marker_texts[0].lower()
    # "abandoned" framing + an explicit do-not-continue instruction are what
    # make the model drop the canceled request. The old marker had neither;
    # asserting both fails loud if the disregard semantics regress.
    assert "abandon" in marker, (
        f"Marker must frame the prior request as abandoned, got: {marker_texts[0]!r}"
    )
    assert "do not resume or act on" in marker, (
        f"Marker must instruct the model not to continue the interrupted "
        f"request, got: {marker_texts[0]!r}"
    )
    # The original assistant-incomplete disclaimer is still useful context
    # and must be retained alongside the new instruction.
    assert "may be" in marker and "incomplete" in marker, (
        f"Marker should still note the assistant message may be incomplete, "
        f"got: {marker_texts[0]!r}"
    )
    assert marker_texts[0].endswith(f"\n{line}")


class _WakeRecordingServerClient(NullServerClient):
    """Records the runner→AP wake POSTs a parent session's ``/events`` receives.

    Subclasses :class:`NullServerClient` so every other runner→AP call still
    gets a benign empty 200; only POSTs to the watched parent's ``/events``
    path are captured. ``wake_seen`` lets a test await the (background) wake
    deterministically instead of sleeping.
    """

    def __init__(self, parent_id: str) -> None:
        """
        :param parent_id: Parent session whose ``/events`` POSTs to capture,
            e.g. ``"bf881b8f7e32add48bfcd6afc476452a"``.
        """
        self._parent_events_path = f"/v1/sessions/{parent_id}/events"
        self.wake_posts: list[dict[str, Any]] = []
        self.wake_seen = asyncio.Event()

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Capture a wake POST to the watched parent, else defer to the base.

        :param url: Request URL, e.g. ``"/v1/sessions/bf881b8f7e32add48bfcd6afc476452a/events"``.
        :param kwargs: Request kwargs; the wake notice is in ``json``.
        :returns: Stub 200 response from :class:`NullServerClient`.
        """
        if url == self._parent_events_path:
            body = kwargs.get("json")
            if isinstance(body, dict):
                self.wake_posts.append(body)
            self.wake_seen.set()
        return await super().post(url, **kwargs)


def test_subagent_terminal_delivery_retry_uses_latest_undelivered_report() -> None:
    """
    Terminal retry delivers the latest report after the parent inbox reappears.

    A first terminal report can arrive while runner-local parent state is
    missing. The work entry must stay undelivered, but if a later terminal
    report carries newer status/output before the parent inbox returns, the
    parent should receive that latest report rather than stale cancellation
    text from the first failed delivery attempt.
    """
    from omnigent.runner import app as runner_app

    parent_id = "05f117c03074f5d4b0ebe450f79b0684"
    child_id = "01a9880c1386637a7d0a154ecb2c4a72"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="retry",
    )

    try:
        first_ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        runner_app._session_inboxes_ref[parent_id] = session_inbox
        second_ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="completed",
            output="DONE_AFTER_RETRY",
        )
        entry = runner_app.get_subagent_work(child_id)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert first_ack.reason == "missing_parent_inbox"
    assert first_ack.delivered is False
    assert first_ack.delivered_now is False
    assert second_ack.reason == "delivered"
    assert second_ack.delivered is True
    assert second_ack.delivered_now is True
    assert entry is not None
    assert entry.delivered is True
    assert session_inbox.qsize() == 1
    delivered = session_inbox.get_nowait()
    assert delivered["task_id"] == child_id
    assert delivered["status"] == "completed"
    assert delivered["output"] == "DONE_AFTER_RETRY"


def test_subagent_terminal_delivery_handles_missing_output() -> None:
    """
    Terminal work with no assistant text still delivers a marker payload.

    Native status reporters can emit ``idle`` without a final assistant
    message. That must not become an unstructured ``RuntimeError`` after the
    parent inbox is available.
    """
    from omnigent.runner import app as runner_app

    parent_id = "9c98fbe12742d712819dd26553a7a9ee"
    child_id = "c2a7357e26dc400e4aa6e5c25c611853"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="empty-output",
    )

    try:
        ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="completed",
            output=None,
        )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert ack.reason == "delivered"
    assert ack.delivered is True
    assert session_inbox.qsize() == 1
    delivered = session_inbox.get_nowait()
    assert delivered["output"] == "[System: sub-agent completed with no output]"
