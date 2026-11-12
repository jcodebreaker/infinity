"""Tests for native session model, effort, compact, and plan-mode events."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


@pytest.fixture(autouse=True)
def _isolate_anthropic_default_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ambient ``ANTHROPIC_DEFAULT_*_MODEL`` gateway pins (#4279).

    claude-native model resolution reads these from ``os.environ``; a developer
    whose shell pins them (anyone driving Claude through a gateway) otherwise
    gets a different model-change verdict and these tests fail spuriously.
    """
    for var in (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def _body_carries_text(body: dict[str, Any], needle: str) -> bool:
    """Return whether *needle* appears in any ``input_text`` block of *body*.

    The runner forwards history as nested ``message`` items, so the
    ``input_text`` block carrying the text can sit one or more levels deep
    inside ``content`` lists; walk them recursively.

    :param body: A harness request body (from ``posted_bodies``).
    :param needle: Substring to search for in ``input_text`` blocks.
    :returns: ``True`` if any ``input_text`` block contains *needle*.
    """

    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("type") == "input_text" and needle in (node.get("text") or ""):
                return True
            return _walk(node.get("content"))
        if isinstance(node, list):
            return any(_walk(child) for child in node)
        return False

    return _walk(body.get("content"))


def _turn_bodies(hc: Any) -> list[dict[str, Any]]:
    """The harness posts that are turns: session init's empty CLI pre-launch is not one."""
    return [b for b in hc.posted_bodies if b.get("content") != []]


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_session_dispatches_compact_turn() -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a claude-sdk session
    dispatches a ``/compact`` turn to the harness and returns 200.

    The Claude SDK owns its own context window in the harness subprocess;
    the only effective way to compact it is to send the literal ``/compact``
    slash command to the live client, which triggers native compaction (the
    same PreCompact path the auto-compact machinery already handles and whose
    ``response.compaction.completed`` the executor already emits). The runner
    turns the compact control into a resumed ``/compact`` turn and forwards it
    to the harness like any user message.

    The 200 (not 204) is load-bearing: the Omnigent server reads it to know
    the harness handled the control and skips its own (transcript-only,
    ineffective for SDK) compaction. A regression returning 204 would make
    the server surface a 400 "not available for this session type" error.
    """
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)

    sdk_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the claude-sdk spec for any agent_id."""
        del agent_id, session_id
        return sdk_spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    sid = "b1d0f6a2c3e14f5a9b8c7d6e5f403122"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "compact"},
        )

        # The turn runs in the background; wait for it to forward the
        # synthesized /compact message to the harness. Session init's empty
        # CLI pre-launch post (ADR 0026) is not a turn.
        def _turns() -> list[dict[str, Any]]:
            return [b for b in hc.posted_bodies if b.get("content") != []]

        for _ in range(100):
            if _turns():
                break
            await asyncio.sleep(0.02)

    # 200 = handled by the harness (server skips its own compaction).
    # 204 would make the server return 400 "not available for this
    # session type" — the regression this pins against.
    assert resp.status_code == 200, (
        f"claude-sdk compact must return 200 (handled) from /events; "
        f"got {resp.status_code}: {resp.text}"
    )
    # Exactly one forwarded turn. 0 = compact didn't dispatch; 2+ = it
    # dispatched more than a single compact turn.
    turns = _turns()
    assert len(turns) == 1, (
        f"Expected exactly one forwarded turn from claude-sdk compact, got {len(turns)}: {turns}"
    )
    # Body contract: the literal ``/compact`` is what the Claude CLI
    # interprets as the slash command that runs native compaction. Plain
    # prompt text (missing slash) would land as a normal chat turn instead.
    assert _body_carries_text(turns[0], "/compact"), (
        f"The forwarded turn must carry the literal '/compact' slash command "
        f"so the SDK runs native compaction; got {turns[0]}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_buffers_behind_active_turn() -> None:
    """
    Compact on a busy claude-sdk session buffers ``/compact`` for the next
    turn instead of starting a second concurrent one.

    The harness holds a single live client per conversation, so a ``/compact``
    issued while a turn is in flight must not race it. The runner buffers the
    synthesized ``/compact`` message; the normal continuation drain runs it
    once the active turn ends. It still returns 200 (handled) so the server
    skips its own compaction.
    """
    hc = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(hc)

    sdk_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the claude-sdk spec for any agent_id."""
        del agent_id, session_id
        return sdk_spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    sid = "c2e1a7b3d4f05a6b8c9d0e1f20314233"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        # Simulate an in-flight turn holding the slot (a non-Task sentinel so
        # nothing tries to await/cancel it during teardown).
        active_turns = app.state.active_turns
        sentinel = object()
        active_turns[sid] = sentinel

        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "compact"},
        )

    # 200 = handled (buffered for the next turn); the server skips its own
    # compaction just as it would for an immediately-dispatched compact.
    assert resp.status_code == 200, (
        f"claude-sdk compact on a busy session must still return 200; "
        f"got {resp.status_code}: {resp.text}"
    )
    # The active slot must be untouched — no second turn was started.
    assert app.state.active_turns.get(sid) is sentinel, (
        "compact must not start a second turn while one is in flight; the "
        "active-turn slot was replaced."
    )
    # The /compact message was buffered for the continuation drain.
    buffered = app.state.session_message_buffers.get(sid) or []
    assert len(buffered) == 1, f"Expected exactly one buffered /compact message, got {buffered!r}."
    assert _body_carries_text(buffered[0], "/compact"), (
        f"Buffered message must carry the literal '/compact'; got {buffered[0]!r}."
    )
    # Nothing was forwarded to the harness yet — it runs after the active turn.
    assert _turn_bodies(hc) == [], (
        f"Buffered compact must not forward to the harness immediately; got {_turn_bodies(hc)!r}."
    )


async def _accumulate_session_events(
    sid: str,
    *,
    until_types: set[str],
    timeout_s: float = 3.0,
) -> list[dict[str, Any]]:
    """Drain a session's event queue repeatedly until a terminal type appears.

    The compact turn runs in the background, so its events land on the queue
    over time. Accumulate across polls (the drain is destructive) and stop once
    any of ``until_types`` is seen or the timeout elapses.

    :param sid: Conversation id whose queue to drain.
    :param until_types: Event ``type`` values that end the wait.
    :param timeout_s: Upper bound on how long to poll.
    :returns: Every dict event seen, in arrival order.
    """
    from omnigent.runner.app import _session_event_queues_ref

    seen: list[dict[str, Any]] = []
    waited = 0.0
    while waited < timeout_s:
        seen.extend(_drain_session_event_queue(_session_event_queues_ref.get(sid)))
        if any(e.get("type") in until_types for e in seen):
            return seen
        await asyncio.sleep(0.02)
        waited += 0.02
    return seen


def _build_claude_sdk_compact_app(
    sse_frames: list[str],
) -> tuple[Any, _FakeProcessManager, _ScriptedHarnessClient]:
    """Wire a runner app whose claude-sdk turn streams *sse_frames*.

    :param sse_frames: The SSE frames the scripted harness relays for the
        dispatched ``/compact`` turn.
    :returns: ``(app, process_manager, harness_client)``.
    """
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)

    sdk_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return sdk_spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, hc


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_publishes_in_progress_up_front() -> None:
    """A claude-sdk `/compact` publishes `response.compaction.in_progress` at once.

    Native `/compact` shows a "Compacting conversation…" spinner immediately;
    the claude-sdk turn otherwise only surfaces the generic running status
    ("Working…") until the executor's own late `in_progress` (fired when the
    SDK PreCompact hook hits). Publishing up front gives the same instant
    feedback as the other harnesses.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, _hc = _build_claude_sdk_compact_app(frames)

    sid = "d3f0a1b2c3d4e5f60718293a4b5c6d7e"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text

        events = await _accumulate_session_events(
            sid, until_types={"response.compaction.in_progress"}
        )

    in_progress = [e for e in events if e.get("type") == "response.compaction.in_progress"]
    assert in_progress, (
        f"claude-sdk compact must publish response.compaction.in_progress up front so the "
        f"'Compacting conversation…' spinner shows immediately; got events {events!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_swallows_executor_duplicate_in_progress() -> None:
    """The executor's own `in_progress` is swallowed so the web shows one spinner.

    The runner publishes an up-front `in_progress`; the executor then emits its
    own when the PreCompact hook fires. Two spinners would leave one stranded
    (the web removes only a single `compaction_loading` on `completed`), so the
    relay must drop the executor's duplicate. On a real compaction (a
    `completed` arrives), no `failed` is published.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.compaction.in_progress"}),
        _sse(
            {
                "type": "response.compaction.completed",
                "summary": "compacted",
                "total_tokens": 1234,
                "compacted_messages": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                    }
                ],
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, _hc = _build_claude_sdk_compact_app(frames)

    sid = "e4a1b2c3d4e5f60718293a4b5c6d7e8f"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text

        events = await _accumulate_session_events(
            sid, until_types={"response.compaction.completed"}
        )

    in_progress = [e for e in events if e.get("type") == "response.compaction.in_progress"]
    completed = [e for e in events if e.get("type") == "response.compaction.completed"]
    failed = [e for e in events if e.get("type") == "response.compaction.failed"]
    assert len(in_progress) == 1, (
        f"Exactly one in_progress must reach the web (up-front published, executor's "
        f"duplicate swallowed); got {len(in_progress)}: {events!r}"
    )
    assert len(completed) == 1, f"Expected the completed event to pass through; got {events!r}"
    assert failed == [], (
        f"No failed must be published when compaction actually completes; got {events!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_publishes_failed_when_no_compaction() -> None:
    """When the `/compact` turn ends with no compaction, publish `failed`.

    The up-front spinner is only cleared by `completed` (→ marker) or `failed`
    (→ removed). If the turn ends without the executor reporting a compaction
    (e.g. nothing to compact), the runner must publish `failed` so the spinner
    is not stranded.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, _hc = _build_claude_sdk_compact_app(frames)

    sid = "f5b2c3d4e5f60718293a4b5c6d7e8f90"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text

        events = await _accumulate_session_events(sid, until_types={"response.compaction.failed"})

    failed = [e for e in events if e.get("type") == "response.compaction.failed"]
    completed = [e for e in events if e.get("type") == "response.compaction.completed"]
    assert failed, (
        f"A compact turn that ends without a compaction must publish failed to clear the "
        f"stranded spinner; got {events!r}"
    )
    assert completed == [], f"No completed should appear when nothing compacted; got {events!r}"


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_clears_flag_on_pre_stream_failure() -> None:
    """A `/compact` turn that fails before streaming still clears the spinner.

    The up-front `response.compaction.in_progress` and the in-progress flag are
    set before the turn task runs, but the loop that clears them runs only once
    streaming starts. A setup-phase failure (here: no live harness) ends the turn
    before that loop, so cleanup cannot live only inside the stream loop:
    `_on_proxy_stream_end` — reached on every turn-end path — must publish
    `failed` and discard the flag. Otherwise the spinner is stranded and the
    stale flag corrupts the NEXT turn's compaction signalling (a real
    `in_progress` is swallowed, or an unrelated turn emits a spurious `failed`).
    """

    class _FailingProcessManager(_FakeProcessManager):
        """Process manager that fails to hand out a client once armed."""

        def __init__(self, client: _ScriptedHarnessClient) -> None:
            super().__init__(client)
            self.fail_get_client = False

        async def get_client(
            self, conversation_id: str, harness: str, env: Any = None
        ) -> _ScriptedHarnessClient:
            if self.fail_get_client:
                raise RuntimeError("no live harness")
            return await super().get_client(conversation_id, harness, env)

    hc = _ScriptedHarnessClient([])
    pm = _FailingProcessManager(hc)
    sdk_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return sdk_spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    sid = "a6c3d4e5f60718293a4b5c6d7e8f9012"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        # Arm the setup-phase failure, then dispatch /compact. The handler still
        # returns 200 (the turn runs in the background); the failure lands inside
        # the turn task, before any streaming.
        pm.fail_get_client = True
        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text

        events = await _accumulate_session_events(sid, until_types={"response.compaction.failed"})

    failed = [e for e in events if e.get("type") == "response.compaction.failed"]
    assert failed, (
        f"A /compact turn that fails during setup (before streaming) must still publish "
        f"failed to clear the up-front spinner; got {events!r}"
    )
    # The flag must not leak past the turn: a stale entry would swallow the next
    # turn's real in_progress or make an unrelated turn emit a spurious failed.
    assert sid not in app.state.sdk_compact_inprogress, (
        "the in-progress flag must be cleared on the setup-failure path; a leaked "
        "entry corrupts later compaction signalling for this conversation."
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_buffered_compact_dispatches_standalone() -> None:
    """A buffered /compact ahead of a follow-up message still dispatches on its own.

    The non-native continuation drain coalesces the whole buffer and dispatches
    only the last body. Left unguarded, a /compact buffered behind an active turn
    with a user message queued after it would be buried in history (never the
    turn prompt) and silently no-op — the runner already returned 200, so the
    server won't fall back. The drain must dispatch a buffered /compact as its
    own turn instead.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, hc = _build_claude_sdk_compact_app(frames)

    sid = "b7d4e5f60718293a4b5c6d7e8f901234"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        # Post-active-turn state: a /compact buffered first, then a follow-up
        # user message queued after it (the ordering that would bury /compact).
        app.state.session_message_buffers[sid] = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "/compact"}],
                "conversation_id": sid,
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "a follow-up message"}],
                "conversation_id": sid,
            },
        ]

        # Drain as the prior turn's continuation would, then wait for the turns
        # it dispatches to reach the harness.
        await app.state.check_and_start_next_turn(sid)

        for _ in range(200):
            if len(_turn_bodies(hc)) >= 2:
                break
            await asyncio.sleep(0.02)

    # The fix drains one at a time when a /compact is buffered, so /compact
    # dispatches as its OWN turn and the follow-up as a SECOND turn (the scripted
    # /compact turn completes immediately, kicking off the next drain) → two
    # dispatches. The buggy coalescing drain folds both into ONE turn (follow-up
    # as the prompt, /compact buried in history, never run as a slash command)
    # → a single dispatch. Assert on the count, not body content: the harness
    # fake captures the shared history list by reference, so a later append is
    # visible on an already-captured body.
    n = len(_turn_bodies(hc))
    assert n == 2, (
        "a buffered /compact must dispatch as its own turn (two dispatches: /compact, then "
        f"the follow-up), not be coalesced into one; got {n} dispatch(es)"
    )
    assert _body_carries_text(_turn_bodies(hc)[0], "/compact"), (
        "the first dispatched turn must carry the /compact command"
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_buffered_compact_failed_fallback() -> None:
    """A buffered /compact that drains and ends with no compaction still clears the spinner.

    When a buffered /compact dispatches as its own turn, the executor can emit
    `response.compaction.in_progress` (the PreCompact hook) without a matching
    `response.compaction.completed` (e.g. the compaction-complete event resolves
    to None). The buffered path must set `_sdk_compact_inprogress` like the idle
    path so `_on_proxy_stream_end` publishes `response.compaction.failed` and the
    "Compacting…" spinner is not stranded — and so the relay swallows the
    executor's own in_progress, leaving exactly one spinner on the web.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.compaction.in_progress"}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, _hc = _build_claude_sdk_compact_app(frames)

    sid = "c8e5f60718293a4b5c6d7e8f90123456"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        # A /compact buffered behind an active turn drains as its own turn.
        app.state.session_message_buffers[sid] = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "/compact"}],
                "conversation_id": sid,
            },
        ]
        await app.state.check_and_start_next_turn(sid)

        events = await _accumulate_session_events(sid, until_types={"response.compaction.failed"})

    failed = [e for e in events if e.get("type") == "response.compaction.failed"]
    in_progress = [e for e in events if e.get("type") == "response.compaction.in_progress"]
    completed = [e for e in events if e.get("type") == "response.compaction.completed"]
    assert failed, (
        "a buffered /compact that ends without a compaction must publish failed to clear the "
        "spinner (the busy path must set the in-progress flag like the idle path); "
        f"got types {[e.get('type') for e in events]}"
    )
    assert len(in_progress) == 1, (
        "exactly one in_progress must reach the web (up-front published, executor's duplicate "
        f"swallowed); got {len(in_progress)} from types {[e.get('type') for e in events]}"
    )
    assert completed == [], "no completed should appear when nothing compacted"
