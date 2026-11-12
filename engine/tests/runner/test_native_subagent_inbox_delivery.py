"""Sub-agent completions must reach the parent inbox exactly once.

Covers restart recovery (a fresh runner rebuilds undrained child results from
the server's child-session receipts), the resumed child's continuation turn,
and a routed child whose spec names another harness. The native CLI forwarder
path (``external_session_status`` events) was removed with the native harnesses
(ADR 0020).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)

# Reuse the proven runner-turn stubs from the sessions-native suite.
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_orchestrator"
CHILD_SESSION_ID = "conv_child_reviewer"


@pytest.fixture
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide sub-agent / inbox maps.

    The sub-agent work registry and inbox queues live in module-level dicts on
    ``omnigent.runner.app`` that otherwise leak across tests. Clear them before
    the test and restore the originals after.
    """
    saved = (
        dict(runner_app._subagent_work_by_child),
        {k: set(v) for k, v in runner_app._subagent_work_by_parent.items()},
        dict(runner_app._session_inboxes_ref),
        set(runner_app._drained_delivered_subagent_children),
        set(runner_app._subagent_recovery_done),
        dict(runner_app._subagent_recovery_locks),
    )
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    runner_app._drained_delivered_subagent_children.clear()
    runner_app._subagent_recovery_done.clear()
    runner_app._subagent_recovery_locks.clear()
    try:
        yield
    finally:
        runner_app._subagent_work_by_child.clear()
        runner_app._subagent_work_by_child.update(saved[0])
        runner_app._subagent_work_by_parent.clear()
        runner_app._subagent_work_by_parent.update(saved[1])
        runner_app._session_inboxes_ref.clear()
        runner_app._session_inboxes_ref.update(saved[2])
        runner_app._drained_delivered_subagent_children.clear()
        runner_app._drained_delivered_subagent_children.update(saved[3])
        runner_app._subagent_recovery_done.clear()
        runner_app._subagent_recovery_done.update(saved[4])
        runner_app._subagent_recovery_locks.clear()
        runner_app._subagent_recovery_locks.update(saved[5])


DISPATCH_ID = "subagent_dispatch0001"
_CHILD_RESULT_ITEM: dict[str, Any] = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "review complete: LGTM"}],
}


def _child_summary(**overrides: Any) -> dict[str, Any]:
    """
    Build a terminal child-session summary as the sessions API returns it.

    :param overrides: Field overrides, e.g. ``current_task_status="failed"``.
    :returns: Child summary carrying an undrained dispatch id by default.
    """
    summary: dict[str, Any] = {
        "id": CHILD_SESSION_ID,
        "tool": "reviewer",
        "session_name": "review",
        "current_task_status": "completed",
        "labels": {runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID},
    }
    summary.update(overrides)
    return summary


class _RecoveryServerClient(NullServerClient):
    """Serve the durable child records that restart recovery reads."""

    def __init__(
        self,
        children: list[dict[str, Any]],
        *,
        child_items: list[dict[str, Any]] | None = None,
        failed_item_sessions: set[str] | None = None,
    ) -> None:
        """
        Configure the child list and transcript returned by the fake server.

        :param children: Parent's child-session summaries.
        :param child_items: Child transcript, newest first.
        :param failed_item_sessions: Session ids whose item read returns 503.
        """
        self.children = children
        self.child_items = [_CHILD_RESULT_ITEM] if child_items is None else child_items
        self.failed_item_sessions = failed_item_sessions or set()
        self.requests: list[tuple[str, dict[str, Any]]] = []

    class _Resp:
        """Minimal HTTP response carrying a JSON payload."""

        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            """
            Store one JSON response payload.

            :param payload: JSON object returned by :meth:`json`.
            :param status_code: HTTP status exposed to production code.
            """
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, Any]:
            """Return the configured JSON payload."""
            return self._payload

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return child summaries and transcripts for recovery reads.

        :param url: Requested sessions API path.
        :param kwargs: HTTP request options; ``params`` are recorded.
        :returns: Minimal response for the requested resource.
        """
        params = dict(kwargs.get("params") or {})
        self.requests.append((url, params))
        if url.endswith(f"/{PARENT_SESSION_ID}/child_sessions"):
            return self._Resp({"data": self.children, "has_more": False})
        if url.endswith("/items"):
            session_id = url.rstrip("/").split("/")[-2]
            if session_id in self.failed_item_sessions:
                return self._Resp({}, status_code=503)
            return self._Resp({"data": self.child_items, "has_more": False})
        return self._Resp({"data": [], "has_more": False})


@pytest.mark.asyncio
async def test_runner_restart_recovers_undrained_terminal_child(
    _clean_subagent_registry: None,
) -> None:
    """A fresh runner rebuilds an undrained child result from the receipt gap.

    Nothing survives the process: no work entry, inbox item, or drained
    tombstone. The child carries a dispatch id but no delivered-id receipt,
    so initializing the parent must re-queue its result under that same id.
    """
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the parent spec needed by the public initialization route.

        :param agent_id: Bound agent id supplied by initialization.
        :param session_id: Parent session id being initialized.
        :returns: Minimal parent agent specification.
        """
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="orchestrator")

    server_client = _RecoveryServerClient([_child_summary()])
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions",
            json={"session_id": PARENT_SESSION_ID, "agent_id": "ag_orchestrator"},
        )

    assert response.status_code == 201
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    assert inbox.qsize() == 1
    payload = inbox.get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "completed"
    assert payload["output"] == "review complete: LGTM"
    assert payload["work_id"] == DISPATCH_ID
    child_reads = [
        params
        for url, params in server_client.requests
        if url.endswith(f"/{CHILD_SESSION_ID}/items")
    ]
    assert child_reads == [{"limit": "100", "order": "desc"}]
    request_count = len(server_client.requests)
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert len(server_client.requests) == request_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        {
            runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
            runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY: DISPATCH_ID,
        },
        {},
    ],
)
async def test_runner_restart_skips_drained_and_unstamped_children(
    _clean_subagent_registry: None,
    labels: dict[str, str],
) -> None:
    """A matching receipt, or a child created before receipts, is not replayed.

    :param _clean_subagent_registry: Isolates module-level runner state.
    :param labels: Either a drained turn's labels or a legacy child's none.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient([_child_summary(labels=labels)])
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None
    assert not any(url.endswith("/items") for url, _ in server_client.requests)


@pytest.mark.asyncio
async def test_runner_restart_replays_continued_turn_with_stale_receipt(
    _clean_subagent_registry: None,
) -> None:
    """A receipt for an earlier turn cannot mask a continued child's new turn."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    labels = {
        runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: "subagent_turn2",
        runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY: "subagent_turn1",
    }
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary(labels=labels)]),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["work_id"] == "subagent_turn2"
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_drain_before_session_init_still_recovers(
    _clean_subagent_registry: None,
) -> None:
    """A drain that runs before the session is initialized still recovers.

    After a reconnect the server can dispatch a pending message before it
    re-initializes the session on the replacement runner, so the parent has no
    inbox yet when ``sys_read_inbox`` runs. Recovery must create the inbox and
    queue the result rather than treat the missing inbox as nothing to do.
    """
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary()]),  # type: ignore[arg-type]
    )
    assert PARENT_SESSION_ID not in runner_app._session_inboxes_ref

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_runner_restart_recovers_text_less_final_turn_as_no_output(
    _clean_subagent_registry: None,
) -> None:
    """The newest assistant message wins even without text, as in live delivery.

    Walking past it to an older message would surface a previous turn's text
    as this turn's result.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    child_items = [
        {"type": "message", "role": "assistant", "content": []},
        _CHILD_RESULT_ITEM,
    ]
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary()], child_items=child_items),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == "completed"
    assert payload["output"] == ""


@pytest.mark.asyncio
async def test_runner_restart_recovers_failed_child_error(
    _clean_subagent_registry: None,
) -> None:
    """A failed child replays its durable error without reading its transcript."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        [
            _child_summary(
                current_task_status="failed",
                last_task_error={"code": "required_terminal_exited", "message": "pane died"},
            )
        ]
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == "failed"
    assert payload["output"] == "pane died"
    assert not any(url.endswith("/items") for url, _ in server_client.requests)


@pytest.mark.asyncio
async def test_runner_restart_retries_after_child_history_read_failure(
    _clean_subagent_registry: None,
) -> None:
    """A failed transcript read leaves the scan unfinished so the drain retries it."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        [_child_summary()], failed_item_sessions={CHILD_SESSION_ID}
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert PARENT_SESSION_ID not in runner_app._subagent_recovery_done

    server_client.failed_item_sessions.clear()
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert PARENT_SESSION_ID in runner_app._subagent_recovery_done


@pytest.mark.asyncio
async def test_concurrent_recovery_scans_deliver_exactly_once(
    _clean_subagent_registry: None,
) -> None:
    """Session initialization racing a ``sys_read_inbox`` drain queues one result."""

    class _YieldingRecoveryServerClient(_RecoveryServerClient):
        """Yield to the event loop on every read, exposing the interleaving."""

        async def get(self, url: str, **kwargs: Any) -> Any:
            await asyncio.sleep(0)
            return await super().get(url, **kwargs)

    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app = create_runner_app(
        server_client=_YieldingRecoveryServerClient([_child_summary()]),  # type: ignore[arg-type]
    )

    await asyncio.gather(
        app.state.recover_undrained_subagent_results(PARENT_SESSION_ID),
        app.state.recover_undrained_subagent_results(PARENT_SESSION_ID),
    )

    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].qsize() == 1


@pytest.mark.asyncio
async def test_routed_child_off_its_native_spec_still_delivers(
    _clean_subagent_registry: None,
) -> None:
    """A child routed onto an SDK harness delivers, though its spec says native.

    The Smart Routing shape: polly's ``claude_code`` worker declares
    ``claude-native``, but a routed child is forwarded ``harness_override`` and
    actually runs ``claude-sdk``. The runner read the harness off the cached
    SPEC, so the turn looked native and its completion was left to a native
    path that never runs — the parent waited forever while the pi sibling
    (whose spec harness is already non-native) was the only one to report.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="claude_code",
        title="joke-claude",
    )
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.output_text.delta", "delta": "knock knock"}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="claude_code",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "message",
                "agent_id": "ag_reviewer",
                "harness_override": "claude-sdk",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "tell me a joke"}],
                },
            },
        )
        assert resp.status_code in (200, 202), resp.text
        for _ in range(200):
            if not inbox.empty():
                break
            await asyncio.sleep(0.01)

    items: list[dict[str, Any]] = []
    while not inbox.empty():
        items.append(inbox.get_nowait())
    assert items, (
        "a routed child finished its turn but nothing reached the parent inbox: "
        "the runner read the harness off the spec (claude-native) instead of the "
        "forwarded harness_override (claude-sdk)"
    )
    assert items[0]["status"] == "completed"
    assert items[0]["conversation_id"] == CHILD_SESSION_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [None, "runner_disconnected", "runner_failed_to_start"])
@pytest.mark.parametrize("previous_execution", ["new", "finished", "messaged"])
async def test_recovered_child_continues_same_dispatch_before_delivering_result(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str | None,
    previous_execution: str,
) -> None:
    """Parent initialization keeps the child pending; child init resumes its task once."""
    from omnigent.entities import Conversation
    from omnigent.runner.session_init_protocol import build_runner_session_init_payload
    from tests.runner.conftest import _sse

    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_recovered"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_recovered"}}),
        ]
    )

    def turns() -> list[dict[str, Any]]:
        # Session init's empty CLI pre-launch post is not a turn.
        return [b for b in harness.posted_bodies if b.get("content") != []]

    started, release = asyncio.Event(), asyncio.Event()
    if previous_execution == "messaged":
        original = _ScriptedHarnessClient._StreamHandle.aiter_text

        async def gated_stream(handle: Any) -> Any:
            started.set()
            await release.wait()
            async for frame in original(handle):
                yield frame

        monkeypatch.setattr(_ScriptedHarnessClient._StreamHandle, "aiter_text", gated_stream)
    pm = _FakeProcessManager(harness)
    server = _RecoveryServerClient(
        [
            _child_summary(
                current_task_status="failed" if error_code else "in_progress",
                last_task_error={"code": error_code, "message": "runner lost"},
            )
        ]
    )

    async def resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        if agent_id == "ag_reviewer":
            return AgentSpec(
                spec_version=1,
                name="worker",
                executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
            )
        return AgentSpec(spec_version=1, name="orchestrator")

    resources = runner_app.SessionResourceRegistry()
    app = create_runner_app(
        resource_registry=resources,
        process_manager=pm,
        spec_resolver=resolve,
        server_client=server,  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        parent = await client.post(
            "/v1/sessions", json={"session_id": PARENT_SESSION_ID, "agent_id": "ag_orchestrator"}
        )
        assert parent.status_code == 201, parent.text
        inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
        entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
        assert entry is not None and entry.work_id == DISPATCH_ID
        assert entry.status not in {"failed", "completed", "cancelled"}
        assert inbox.empty(), "runner crash was delivered as a finished child result"
        child = Conversation(
            id=CHILD_SESSION_ID,
            agent_id="ag_reviewer",
            runner_id="replacement",
            root_conversation_id=PARENT_SESSION_ID,
            parent_conversation_id=PARENT_SESSION_ID,
            created_at=0,
            updated_at=0,
        )
        payload = build_runner_session_init_payload(
            child,
            server_version="test",
            resume_interrupted_turn=True,
        )
        if previous_execution == "finished":
            # Runner A retains its old turn epoch while this child ran on B.
            app.state.begin_turn_slot(CHILD_SESSION_ID)
            app.state.active_turns.pop(CHILD_SESSION_ID)
        if previous_execution == "messaged":
            message = await client.post(
                f"/v1/sessions/{CHILD_SESSION_ID}/events",
                json={
                    "type": "message",
                    "agent_id": "ag_reviewer",
                    "content": [{"type": "input_text", "text": "new user instruction"}],
                },
            )
            assert message.status_code == 202, message.text
            await asyncio.wait_for(started.wait(), timeout=5)
        for _ in range(2):
            result = await client.post("/v1/sessions", json=payload)
            assert result.status_code == 201
        if previous_execution == "messaged":
            assert len(turns()) == 1
            content = str(turns()[0]["content"])
            assert "new user instruction" in content
            assert "Continue the existing task" not in content
            release.set()
        else:
            for _ in range(100):
                if turns():
                    break
                await asyncio.sleep(0.01)
            assert len(turns()) == 1, "interrupted child must receive one continuation turn"
            assert "Continue the existing task" in str(turns()[0]["content"])
        for _ in range(100):
            if not inbox.empty():
                break
            await asyncio.sleep(0.01)
        assert "review complete: LGTM" in str(turns()[0]["content"])
        result = inbox.get_nowait()
        assert result["conversation_id"] == CHILD_SESSION_ID
        assert result["work_id"] == DISPATCH_ID
        assert result["status"] == "completed"
        turn = app.state.active_turns.get(CHILD_SESSION_ID)
        if turn is not None:
            await turn
        await client.post("/v1/sessions", json=payload)
        assert len(turns()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [None, "runner_disconnected", "runner_failed_to_start"])
async def test_recovered_pending_child_outlives_launch_timeout_and_delivers_original_result(
    _clean_subagent_registry: None, error_code: str | None
) -> None:
    """Recovery must await the original dispatch, including work on another live runner."""
    from unittest.mock import AsyncMock

    from omnigent.runner.tool_dispatch import _cleanup_drained_subagent_work, _drain_inbox

    child = _child_summary(
        runner_id="other-live-runner",
        current_task_status="failed" if error_code else "in_progress",
        last_task_error={"code": error_code, "message": "runner lost"},
    )
    server = _RecoveryServerClient([child])
    server.patch = AsyncMock(return_value=server._Resp({}))
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None and entry.work_id == DISPATCH_ID
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]

    assert (
        runner_app.reap_stalled_subagent_launches(now=entry.created_at + 181, timeout_s=180) == []
    )
    assert inbox.empty()
    await _drain_inbox(inbox, server_client=server, conversation_id=PARENT_SESSION_ID)
    server.patch.assert_not_awaited()

    # No local child running edge: the server later records completion elsewhere.
    child.update(current_task_status="completed", last_task_error=None)
    reconciled = asyncio.Event()

    async def reconcile() -> None:
        await app.state.reconcile_pending_subagent_results()
        reconciled.set()

    sweep = asyncio.create_task(
        runner_app.run_subagent_launch_reaper(interval_s=0.001, reconcile_pending=reconcile)
    )
    try:
        await asyncio.wait_for(reconciled.wait(), timeout=5)
    finally:
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
    assert inbox.qsize() == 1
    payload = inbox.get_nowait()
    assert payload["work_id"] == DISPATCH_ID
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "completed"
    assert payload["output"] == "review complete: LGTM"
    await _cleanup_drained_subagent_work(payload, server_client=server)
    server.patch.assert_awaited_once_with(
        f"/v1/sessions/{CHILD_SESSION_ID}",
        json={"labels": {runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY: DISPATCH_ID}},
        timeout=30.0,
    )
    await app.state.reconcile_pending_subagent_results()
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert inbox.empty()
    late = runner_app.mark_subagent_work_terminal(
        CHILD_SESSION_ID, status="completed", output="review complete: LGTM"
    )
    assert late.delivered and not late.delivered_now
    assert inbox.empty()
    assert child["runner_id"] == "other-live-runner"
