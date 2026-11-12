"""Tests for Sessions API snapshot item pagination."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import StatementError

from omnigent.entities import Conversation, ConversationItem, MessageData, PagedList
from omnigent.server.routes import sessions as _sessions_mod
from omnigent.server.routes.sessions import (
    _LABEL_VALUE_MAX_LEN,
    SessionLiveness,
    _get_session_snapshot,
    _persist_session_status_error_labels,
    _publish_subtree_cost_to_ancestors,
    _truncate_label,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


async def _drain_model_options(session_id: str) -> None:
    """Pump the loop until the background native model-options fetch lands.

    Runner model options are eventually consistent: the first
    snapshot returns ``[]`` and starts the runner query; a later snapshot
    serves the cache.
    """
    for _ in range(100):
        if session_id in _sessions_mod._model_options_cache:
            return
        await asyncio.sleep(0)


def test_snapshot_metadata_resolvers_ignore_malformed_agent_ids() -> None:
    """A wrapped UUID bind error degrades optional snapshot metadata to unknown."""

    class _MalformedAgentStore:
        @staticmethod
        def get(agent_id: str) -> Any:
            raise StatementError(
                "invalid agent id",
                {"agent_id": agent_id},
                ValueError("expected a UUID"),
                False,
            )

    conv = Conversation(
        id="legacy_session",
        created_at=1,
        updated_at=1,
        root_conversation_id="legacy_session",
        agent_id="legacy_agent",
    )
    agent_store = _MalformedAgentStore()
    agent_cache = object()

    assert (
        _sessions_mod._resolve_llm_model(
            conv,
            agent_store=agent_store,
            agent_cache=agent_cache,
        )
        is None
    )
    assert (
        _sessions_mod._resolve_harness(
            conv,
            agent_store=agent_store,
            agent_cache=agent_cache,
        )
        is None
    )


class _ConversationStore:
    """Minimal store that records ``list_items`` calls.

    :param items: Items returned by every ``list_items`` call.
    :param conversations: Optional explicit conversation graph keyed by id,
        used by the subtree-usage tests. When ``None`` (the default), a
        single childless conversation is synthesized per id — preserving the
        original single-session snapshot tests, which have no spawn tree.
    """

    def __init__(
        self,
        items: list[ConversationItem],
        conversations: dict[str, Conversation] | None = None,
    ) -> None:
        self.items = items
        self.list_items_calls: list[dict[str, object]] = []
        self._conversations = conversations

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        if self._conversations is not None:
            return self._conversations.get(conversation_id)
        return Conversation(
            id=conversation_id,
            created_at=1,
            updated_at=1,
            root_conversation_id=conversation_id,
            agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        )

    def list_conversations(
        self,
        *,
        limit: int = 100,
        after: str | None = None,
        kind: str | None = "default",
        root_conversation_id: str | None = None,
        include_archived: bool = False,
    ) -> PagedList[Conversation]:
        """Return the spawn tree sharing ``root_conversation_id``.

        ``load_session_usage`` walks the tree via this method to sum a
        parent's subtree usage, and passes ``include_archived=True`` —
        archived conversations still hold spend. With an explicit graph, return every
        conversation sharing the root; otherwise synthesize the single
        childless conversation the legacy tests expect.
        """
        if self._conversations is not None:
            convs = [
                c
                for c in self._conversations.values()
                if c.root_conversation_id == root_conversation_id
            ]
        else:
            convs = [
                Conversation(
                    id=root_conversation_id or "",
                    created_at=1,
                    updated_at=1,
                    root_conversation_id=root_conversation_id or "",
                    agent_id="087b7cb7ac30abf4debfaa578d052ec6",
                )
            ]
        return PagedList(
            data=convs,
            first_id=convs[0].id if convs else None,
            last_id=convs[-1].id if convs else None,
            has_more=False,
        )

    def list_items(
        self,
        *,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        self.list_items_calls.append(
            {
                "conversation_id": conversation_id,
                "limit": limit,
                "after": after,
                "before": before,
                "order": order,
                "type": type,
            }
        )
        return PagedList(
            data=self.items,
            first_id=self.items[0].id if self.items else None,
            last_id=self.items[-1].id if self.items else None,
            has_more=False,
        )


def _message_item(item_id: str, text: str) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id=f"resp_{item_id}",
        created_at=1,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text}],
        ),
    )


@pytest.mark.asyncio
async def test_session_snapshot_reads_latest_items_then_returns_chronological() -> None:
    """GET /sessions/{id} should not expose the store's oldest-page default."""
    # Model the store response for ``order=desc``: newest first.
    newest_first = [
        _message_item("item_105", "newest"),
        _message_item("item_104", "middle"),
        _message_item("item_103", "oldest in latest page"),
    ]
    conv_store = _ConversationStore(newest_first)

    snapshot = await _get_session_snapshot(conv_store, "e1f7c651c9f97fac088ea70ef633409d")  # type: ignore[arg-type]

    assert conv_store.list_items_calls == [
        {
            "conversation_id": "e1f7c651c9f97fac088ea70ef633409d",
            "limit": 100,
            "after": None,
            "before": None,
            "order": "desc",
            "type": None,
        }
    ]
    assert [item.id for item in snapshot.items] == ["item_103", "item_104", "item_105"]
    assert snapshot.agent_id == "087b7cb7ac30abf4debfaa578d052ec6"
    assert snapshot.status == "idle"


@pytest.mark.asyncio
async def test_session_snapshot_uses_child_spec_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child snapshots expose their selected spec while parents keep the root spec."""
    child_spec = AgentSpec(
        spec_version=1,
        name="executor",
        executor=ExecutorSpec(
            config={"harness": "pi"},
            model="openai-codex/gpt-5.6-sol:medium",
            context_window=100_000,
        ),
    )
    parent_spec = AgentSpec(
        spec_version=1,
        name="advisor",
        executor=ExecutorSpec(
            config={"harness": "pi"},
            model="openai-codex/gpt-5.6-sol:high",
            context_window=200_000,
        ),
        sub_agents=[child_spec],
    )
    conversations = {
        "conv_parent": Conversation(
            id="conv_parent",
            created_at=1,
            updated_at=1,
            root_conversation_id="conv_parent",
            agent_id="ag_advisor",
        ),
        "conv_child": Conversation(
            id="conv_child",
            created_at=1,
            updated_at=1,
            root_conversation_id="conv_parent",
            parent_conversation_id="conv_parent",
            agent_id="ag_advisor",
            kind="sub_agent",
            sub_agent_name="executor",
        ),
    }
    conv_store = _ConversationStore([], conversations=conversations)
    cache_loads: list[bool] = []

    class _AgentStore:
        @staticmethod
        def get(agent_id: str) -> Any:
            assert agent_id == "ag_advisor"
            return type(
                "StoredAgent",
                (),
                {
                    "id": agent_id,
                    "name": "advisor-row",
                    "bundle_location": "bundle",
                    "session_id": None,
                },
            )()

    class _AgentCache:
        @staticmethod
        def load(agent_id: str, bundle_location: str, *, expand_env: bool = False) -> Any:
            assert (agent_id, bundle_location) == ("ag_advisor", "bundle")
            cache_loads.append(expand_env)
            return type("LoadedAgent", (), {"spec": parent_spec})()

    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: None)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    parent = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_parent",
        agent_store=_AgentStore(),  # type: ignore[arg-type]
        agent_cache=_AgentCache(),  # type: ignore[arg-type]
    )
    child = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "conv_child",
        agent_store=_AgentStore(),  # type: ignore[arg-type]
        agent_cache=_AgentCache(),  # type: ignore[arg-type]
    )

    assert parent.agent_name == "advisor"
    assert parent.llm_model == "openai-codex/gpt-5.6-sol:high"
    assert parent.context_window == 200_000
    assert parent.harness == "pi"
    assert child.agent_name == "executor"
    assert child.llm_model == "openai-codex/gpt-5.6-sol:medium"
    assert child.context_window == 100_000
    assert child.harness == "pi"
    assert cache_loads == [True, True, True, True]


@pytest.mark.asyncio
async def test_session_snapshot_unresolvable_sub_agent_warns_and_reports_parent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A child session whose ``sub_agent_name`` no longer resolves in the
    parent bundle publishes the PARENT's identity, model and context window,
    and warns.

    Reporting the parent is long-standing: this path already retained the
    parent spec and published its name, model and context window on a miss.
    What the snapshot did not do was say so. The warning is the new part, and
    it is what makes this the same answer the runner-side consumers of
    ``_find_spec_by_name`` give across a separate process boundary.

    Both halves are asserted: the warning must be emitted AND the parent's
    values must be published — a silent fallback satisfies neither.

    :param monkeypatch: Pytest monkeypatch, used to stub runner lookups.
    :param caplog: Pytest log capture, used to confirm the unresolved
        sub-agent is reported rather than passed over in silence.
    """
    parent_spec = AgentSpec(
        spec_version=1,
        name="advisor",
        executor=ExecutorSpec(
            config={"harness": "pi"},
            model="openai-codex/gpt-5.6-sol:high",
            context_window=200_000,
        ),
        # No sub_agents: "executor" (recorded on the child conversation row)
        # cannot resolve — simulates a spec edit removing the sub-agent
        # after the child session was created.
    )
    conversations = {
        "conv_parent": Conversation(
            id="conv_parent",
            created_at=1,
            updated_at=1,
            root_conversation_id="conv_parent",
            agent_id="ag_advisor",
        ),
        "conv_child": Conversation(
            id="conv_child",
            created_at=1,
            updated_at=1,
            root_conversation_id="conv_parent",
            parent_conversation_id="conv_parent",
            agent_id="ag_advisor",
            kind="sub_agent",
            sub_agent_name="executor",
        ),
    }
    conv_store = _ConversationStore([], conversations=conversations)

    class _AgentStore:
        @staticmethod
        def get(agent_id: str) -> Any:
            assert agent_id == "ag_advisor"
            return type(
                "StoredAgent",
                (),
                {
                    "id": agent_id,
                    "name": "advisor-row",
                    "bundle_location": "bundle",
                    "session_id": None,
                },
            )()

    class _AgentCache:
        @staticmethod
        def load(agent_id: str, bundle_location: str, *, expand_env: bool = True) -> Any:
            assert (agent_id, bundle_location) == ("ag_advisor", "bundle")
            return type("LoadedAgent", (), {"spec": parent_spec})()

    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: None)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    with caplog.at_level(logging.WARNING, logger="omnigent.server.routes._sessions.orchestration"):
        child = await _get_session_snapshot(
            conv_store,  # type: ignore[arg-type]
            "conv_child",
            agent_store=_AgentStore(),  # type: ignore[arg-type]
            agent_cache=_AgentCache(),  # type: ignore[arg-type]
        )

    assert "'executor'" in caplog.text and "did not resolve" in caplog.text, (
        f"The unresolved sub-agent must be warned about; got {caplog.text!r}."
    )
    # The PARENT spec is what the session actually runs on, so it is what the
    # snapshot reports: the spec's own name ("advisor"), not the agent ROW's
    # name ("advisor-row") and not the recorded child name ("executor").
    assert child.agent_name == "advisor"
    assert child.llm_model == "openai-codex/gpt-5.6-sol:high"
    assert child.context_window == 200_000


@pytest.mark.asyncio
async def test_session_snapshot_populates_runner_online_from_session_lookup() -> None:
    """GET /sessions/{id} carries session-scoped runner + host liveness."""
    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    lookup_calls: list[list[str]] = []

    def _liveness_lookup(session_ids: list[str]) -> dict[str, SessionLiveness]:
        """
        Return scripted liveness for the requested session ids.

        :param session_ids: Session ids to resolve, e.g.
            ``["1949523062921b6989466e2fc257925a"]``.
        :returns: Session-scoped split liveness by id.
        """
        lookup_calls.append(session_ids)
        return {
            "1949523062921b6989466e2fc257925a": SessionLiveness(
                runner_online=False, host_online=False
            )
        }

    snapshot = await _get_session_snapshot(
        conv_store,
        "1949523062921b6989466e2fc257925a",  # type: ignore[arg-type]
        liveness_lookup=_liveness_lookup,
    )

    assert lookup_calls == [["1949523062921b6989466e2fc257925a"]]
    assert snapshot.runner_online is False
    assert snapshot.host_online is False


@pytest.mark.asyncio
async def test_session_snapshot_surfaces_runner_exit_report_as_failed() -> None:
    """A crashed runner's exit report surfaces as failed + last_task_error.

    This is the reload-durability leg: the live ``session.status:failed``
    push is gone by the time a page reloads, so the snapshot must read the
    cause from ``RunnerExitReports`` (keyed by the session's runner_id) and
    project it as ``status="failed"`` + ``last_task_error`` — exactly what
    the web's synthetic-error path renders. Without this, a reload after a
    runner crash shows no error.
    """
    from omnigent.server.host_registry import RunnerExitReports

    conv = Conversation(
        id="87876a3cec563d43c2430b633747c7b7",
        created_at=1,
        updated_at=1,
        root_conversation_id="87876a3cec563d43c2430b633747c7b7",
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        runner_id="runner_dead",
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"87876a3cec563d43c2430b633747c7b7": conv},
    )
    reports = RunnerExitReports()
    daemon_error = "runner process exited with code 1\n--- runner log tail ---\nboom"
    reports.record("runner_dead", daemon_error, owner=None)

    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "87876a3cec563d43c2430b633747c7b7",
        runner_exit_reports=reports,
    )

    # Forced to failed by the exit report even though no task ran and the
    # status cache is empty (a fresh crash before any turn).
    assert snapshot.status == "failed"
    assert snapshot.last_task_error is not None
    assert snapshot.last_task_error["code"] == "runner_failed_to_start"
    # The daemon's full cause (incl. log tail) rides through verbatim.
    assert snapshot.last_task_error["message"] == daemon_error


@pytest.mark.asyncio
async def test_session_snapshot_surfaces_status_error_labels_as_last_task_error() -> None:
    """
    A terminal/runtime failure captured from ``session.status`` survives reload.

    Required terminal boot failures can happen before any assistant transcript
    or runner-crash report exists. The live SSE carries ``error``, and the
    relay persists it as labels so a later snapshot can still render a useful
    failure banner instead of ``last_task_error=None``.
    """
    conv = Conversation(
        id="bb66c1adb93f9520bc882bcd05c838e2",
        created_at=1,
        updated_at=1,
        root_conversation_id="bb66c1adb93f9520bc882bcd05c838e2",
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        labels={
            "omnigent.last_task_error_code": "required_terminal_exited",
            "omnigent.last_task_error_message": "Required terminal exited unexpectedly",
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"bb66c1adb93f9520bc882bcd05c838e2": conv},
    )

    snapshot = await _get_session_snapshot(  # type: ignore[arg-type]
        conv_store,
        "bb66c1adb93f9520bc882bcd05c838e2",
    )

    assert snapshot.last_task_error == {
        "code": "required_terminal_exited",
        "message": "Required terminal exited unexpectedly",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "API Error: Request rejected (429) · REQUEST_LIMIT_EXCEEDED",
        'API Error: 429 {"error": {"code": "insufficient_quota"}}',
        "HTTP 429: billing_hard_limit_reached",
        "API Error: 429: Your credit balance is too low to access the API.",
    ],
)
@pytest.mark.parametrize(
    ("stored_code", "expected_code"),
    [
        ("native_turn_error", "rate_limit_exceeded"),
        ("codex_turn_error", "rate_limit_exceeded"),
        ("codex_reauth_required", "codex_reauth_required"),
    ],
)
async def test_session_snapshot_classifies_preexisting_native_rate_limit_errors(
    message: str,
    stored_code: str,
    expected_code: str,
) -> None:
    """Failures saved without a rate-limit code become retryable on reload."""
    session_id = "319c3d34a4ab4e6d983872bf898a19b4"
    conv = Conversation(
        id=session_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=session_id,
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        labels={
            "omnigent.last_task_error_code": stored_code,
            "omnigent.last_task_error_message": message,
        },
    )
    conv_store = _ConversationStore(
        [_message_item("item_rate_limit", message)],
        conversations={session_id: conv},
    )

    snapshot = await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]

    assert snapshot.last_task_error == {"code": expected_code, "message": message}
    assert conv.labels["omnigent.last_task_error_code"] == stored_code


@pytest.mark.asyncio
async def test_session_snapshot_no_exit_report_stays_unfailed() -> None:
    """A session whose runner has no exit report is not marked failed.

    Guards the override from firing for healthy/idle sessions — only a
    recorded crash for THIS session's runner should flip it.
    """
    from omnigent.server.host_registry import RunnerExitReports

    conv = Conversation(
        id="428fdbbaac5e190e6360103acc4fe6c5",
        created_at=1,
        updated_at=1,
        root_conversation_id="428fdbbaac5e190e6360103acc4fe6c5",
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        runner_id="runner_live",
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"428fdbbaac5e190e6360103acc4fe6c5": conv},
    )
    reports = RunnerExitReports()  # empty — no crash recorded

    snapshot = await _get_session_snapshot(
        conv_store,  # type: ignore[arg-type]
        "428fdbbaac5e190e6360103acc4fe6c5",
        runner_exit_reports=reports,
    )

    # No report for runner_live → no forced failure, no synthetic error.
    assert snapshot.status != "failed"
    assert snapshot.last_task_error is None


@pytest.mark.asyncio
async def test_session_snapshot_queries_runner_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _session_status_cache is empty, the snapshot should
    query the runner for live status."""
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()

    # Fake runner client that returns status="running".
    class _FakeResponse:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "running"}

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            return _FakeResponse()

    fake_client = _FakeRunnerClient()
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: fake_client,
    )

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    snapshot = await _get_session_snapshot(
        conv_store,
        "cef2fb55a9d4841cff0b30b2826d91f1",  # type: ignore[arg-type]
    )

    # Runner was queried for this session's status.
    assert "/v1/sessions/cef2fb55a9d4841cff0b30b2826d91f1" in fake_client.get_calls[0]
    assert snapshot.status == "running"

    # Verify the cache is warm: a second call should NOT query the
    # runner again (proves the cache was populated).
    snapshot2 = await _get_session_snapshot(
        conv_store,
        "cef2fb55a9d4841cff0b30b2826d91f1",  # type: ignore[arg-type]
    )
    # Still "running" from the cached value.
    assert snapshot2.status == "running"
    # Status is server-cached, so only the FIRST snapshot queries the
    # runner for status; the second hits the cache.
    status_calls = fake_client.get_calls
    assert len(status_calls) == 1, (
        f"Expected 1 runner status GET (cache hit on second call), "
        f"got {len(status_calls)}. If 2, the cache "
        f"wasn't populated after the first query."
    )


class _HangingRunnerClient:
    """Fake runner whose status GET never answers, like a stalled runner behind a live tunnel."""

    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def get(self, url: str, timeout: float) -> Any:
        self.get_calls.append(url)
        await asyncio.Future()


class _NotFoundRunnerClient:
    """Fake runner that does not know the session."""

    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def get(self, url: str, timeout: float) -> Any:
        self.get_calls.append(url)
        return SimpleNamespace(status_code=404, json=lambda: {"error": "not_found"})


class _GatedRunnerClient:
    """Fake runner whose status GET blocks until released, so callers provably overlap."""

    def __init__(self) -> None:
        self.get_calls: list[str] = []
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, url: str, timeout: float) -> Any:
        self.get_calls.append(url)
        self.arrived.set()
        await self.release.wait()
        return SimpleNamespace(status_code=200, json=lambda: {"status": "running"})


def _use_runner_client(monkeypatch: pytest.MonkeyPatch, runner_client: object) -> None:
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: runner_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)


@pytest.mark.asyncio
async def test_session_snapshot_status_probe_is_bounded_and_backed_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A runner that never answers costs one bounded probe, then none until the backoff ends.

    The tunnel transport ignores httpx timeouts, so an unanswered probe used to
    hold every snapshot of the session until the runner spoke or its tunnel
    dropped, and nothing was cached, so the next snapshot waited again.
    """
    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    session_id = "0a4d2f9c6b1e4e8f9c3a7d5b2e1f0a9c"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    monkeypatch.setattr(orchestration, "_RUNNER_STATUS_PROBE_TIMEOUT_S", 0.05)
    runner_client = _HangingRunnerClient()
    _use_runner_client(monkeypatch, runner_client)
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    with caplog.at_level(logging.WARNING, logger="omnigent.server.routes.sessions"):
        first = await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]
        second = await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]

    assert first.status == "idle"
    assert second.status == "idle"
    # One probe, cut off at the budget; the second snapshot skipped it entirely.
    assert runner_client.get_calls == [f"/v1/sessions/{session_id}"]
    assert _mod._session_status_cache.get(session_id) is None
    assert _mod._runner_status_probe_backoff.get(session_id).failures == 1
    warnings = [r for r in caplog.records if "Runner status probe" in r.getMessage()]
    assert len(warnings) == 1
    assert "no answer within 0.05s" in warnings[0].getMessage()
    assert "skipping it for 30s" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_session_snapshot_concurrent_cache_misses_share_one_status_probe() -> None:
    """Callers racing on a cold status cache await one shared in-flight probe.

    The runner's answer is gated on an event the test controls, so the second
    caller provably attaches while the first probe is still in flight; the pass
    cannot come from two probes merely running back to back.
    """
    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    session_id = "4e8b6d3a0f5c4c2d9a7e9b6f5c3d4e1a"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    runner_client = _GatedRunnerClient()

    first = asyncio.create_task(
        orchestration._probe_runner_live_status(runner_client, session_id)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(runner_client.arrived.wait(), timeout=1.0)
    second = asyncio.create_task(
        orchestration._probe_runner_live_status(runner_client, session_id)  # type: ignore[arg-type]
    )
    # One yield schedules `second` onto the shared probe; only then may it finish.
    await asyncio.sleep(0)
    runner_client.release.set()

    assert await asyncio.gather(first, second) == ["running", "running"]
    assert runner_client.get_calls == [f"/v1/sessions/{session_id}"]
    assert _mod._session_status_cache.get(session_id) == "running"
    assert _mod._runner_status_probe_inflight.get(session_id) is None


@pytest.mark.asyncio
async def test_session_snapshot_cancelled_caller_does_not_abort_shared_status_probe() -> None:
    """Cancelling one caller's snapshot leaves the shared probe running for the rest."""
    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    session_id = "5f9c7e4b1a2d4d3e8b0f1c9d6e5a7b2c"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    runner_client = _GatedRunnerClient()

    first = asyncio.create_task(
        orchestration._probe_runner_live_status(runner_client, session_id)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(runner_client.arrived.wait(), timeout=1.0)
    second = asyncio.create_task(
        orchestration._probe_runner_live_status(runner_client, session_id)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    runner_client.release.set()

    assert await second == "running"
    assert runner_client.get_calls == [f"/v1/sessions/{session_id}"]
    assert _mod._session_status_cache.get(session_id) == "running"


@pytest.mark.asyncio
async def test_session_snapshot_status_probe_resumes_after_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the backoff has elapsed the next snapshot probes the runner again."""
    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    session_id = "1b5e3a0d7c2f4f9a8d4b6e3c2f0a1b8d"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    monkeypatch.setattr(orchestration, "_RUNNER_STATUS_PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(orchestration, "_RUNNER_STATUS_PROBE_BACKOFF_S", 0.0)
    runner_client = _HangingRunnerClient()
    _use_runner_client(monkeypatch, runner_client)
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]
    await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]

    assert len(runner_client.get_calls) == 2


@pytest.mark.asyncio
async def test_session_snapshot_status_probe_asks_again_after_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that does not know the session yet is asked again on the next snapshot.

    A freshly bound runner answers 404 until session init lands; that answer is
    cheap, so it must not put the session in the slow-probe backoff.
    """
    from omnigent.server.routes import sessions as _mod

    session_id = "2c6f4b1e8d3a4a0b9e5c7f4d3a1b2c9e"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    runner_client = _NotFoundRunnerClient()
    _use_runner_client(monkeypatch, runner_client)
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    first = await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]
    second = await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]

    assert first.status == "idle"
    assert second.status == "idle"
    assert len(runner_client.get_calls) == 2
    assert _mod._session_status_cache.get(session_id) is None
    assert _mod._runner_status_probe_backoff.get(session_id) is None


class _SlowNotFoundRunnerClient:
    """Fake runner that answers 404 only after a delay: stalled, and without the session."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.get_calls: list[str] = []

    async def get(self, url: str, timeout: float) -> Any:
        self.get_calls.append(url)
        await asyncio.sleep(self.delay_s)
        return SimpleNamespace(status_code=404, json=lambda: {"error": "not_found"})


@pytest.mark.asyncio
async def test_session_snapshot_slow_non_200_probe_enters_backoff(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-200 that took longer than the slow threshold is treated like a failed probe."""
    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    session_id = "6a0d8f5c2b7e4e4f9c1a3b8d7e6f5a4b"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    monkeypatch.setattr(orchestration, "_RUNNER_STATUS_PROBE_SLOW_S", 0.01)
    runner_client = _SlowNotFoundRunnerClient(delay_s=0.05)
    _use_runner_client(monkeypatch, runner_client)
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    with caplog.at_level(logging.WARNING, logger="omnigent.server.routes.sessions"):
        await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]
        await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]

    assert len(runner_client.get_calls) == 1
    assert _mod._runner_status_probe_backoff.get(session_id).failures == 1
    warnings = [r for r in caplog.records if "Runner status probe" in r.getMessage()]
    assert len(warnings) == 1
    assert "HTTP 404 after" in warnings[0].getMessage()


class _SwitchableRunnerClient:
    """Fake runner that hangs until told to answer 200."""

    def __init__(self) -> None:
        self.get_calls: list[str] = []
        self.answer = False

    async def get(self, url: str, timeout: float) -> Any:
        self.get_calls.append(url)
        if self.answer:
            return SimpleNamespace(status_code=200, json=lambda: {"status": "idle"})
        await asyncio.Future()
        return None


@pytest.mark.asyncio
async def test_session_snapshot_probe_backoff_doubles_per_slow_probe_and_resets_on_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive slow probes double the skip window up to the cap; a prompt answer clears it."""
    import time

    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    session_id = "7b1e9a6d3c8f4f5a0d2b4c9e8f7a6b5c"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    monkeypatch.setattr(orchestration, "_RUNNER_STATUS_PROBE_TIMEOUT_S", 0.05)
    runner_client = _SwitchableRunnerClient()
    _use_runner_client(monkeypatch, runner_client)
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    def _window_s() -> float:
        return _mod._runner_status_probe_backoff.get(session_id).skip_until - time.monotonic()

    def _expire_window() -> None:
        _mod._runner_status_probe_backoff.get(session_id).skip_until = time.monotonic() - 1.0

    expected_windows = [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    for failures, expected in enumerate(expected_windows, start=1):
        await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]
        entry = _mod._runner_status_probe_backoff.get(session_id)
        assert entry.failures == failures
        assert abs(_window_s() - expected) < 2.0, (failures, _window_s())
        _expire_window()
    assert len(runner_client.get_calls) == len(expected_windows)

    runner_client.answer = True
    snapshot = await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]

    assert snapshot.status == "idle"
    assert _mod._session_status_cache.get(session_id) == "idle"
    assert _mod._runner_status_probe_backoff.get(session_id) is None


class _MalformedRunnerClient:
    """Fake runner whose 200 carries a body that is not a JSON object."""

    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def get(self, url: str, timeout: float) -> Any:
        self.get_calls.append(url)
        return SimpleNamespace(status_code=200, json=lambda: ["not", "an", "object"])


@pytest.mark.asyncio
async def test_session_snapshot_malformed_200_probe_fails_softly_for_every_waiter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A 200 without a status object is a failed probe, not an exception every waiter sees."""
    from omnigent.server.routes import sessions as _mod

    session_id = "8c2f0b7e4d9a4a6b1e3c5d0f9a8b7c6d"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    runner_client = _MalformedRunnerClient()
    _use_runner_client(monkeypatch, runner_client)
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    with caplog.at_level(logging.WARNING, logger="omnigent.server.routes.sessions"):
        first, second = await asyncio.gather(
            _get_session_snapshot(conv_store, session_id),  # type: ignore[arg-type]
            _get_session_snapshot(conv_store, session_id),  # type: ignore[arg-type]
        )

    assert (first.status, second.status) == ("idle", "idle")
    assert len(runner_client.get_calls) == 1
    assert _mod._session_status_cache.get(session_id) is None
    assert _mod._runner_status_probe_backoff.get(session_id).failures == 1
    warnings = [r for r in caplog.records if "Runner status probe" in r.getMessage()]
    assert len(warnings) == 1
    assert "malformed body" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_session_snapshot_probe_backoff_is_discarded_when_the_runner_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skip window recorded against one runner does not silence probes of its replacement."""
    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    session_id = "9d3a1c8f6b5e4e7d0f2a4b6c8e1d3f5a"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    stalled = _HangingRunnerClient()

    monkeypatch.setattr(orchestration, "_RUNNER_STATUS_PROBE_TIMEOUT_S", 0.05)
    assert await orchestration._probe_runner_live_status(stalled, session_id, "runner_old") is None  # type: ignore[arg-type]
    assert _mod._runner_status_probe_backoff.get(session_id).runner_id == "runner_old"

    class _HealthyRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float) -> Any:
            self.get_calls.append(url)
            return SimpleNamespace(status_code=200, json=lambda: {"status": "running"})

    replacement = _HealthyRunnerClient()
    status = await orchestration._probe_runner_live_status(replacement, session_id, "runner_new")  # type: ignore[arg-type]

    assert status == "running"
    assert replacement.get_calls == [f"/v1/sessions/{session_id}"]
    assert _mod._runner_status_probe_backoff.get(session_id) is None
    assert _mod._session_status_cache.get(session_id) == "running"


@pytest.mark.asyncio
async def test_session_snapshot_status_probe_bound_holds_over_the_tunnel_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the real tunnel an unanswered probe ends at the budget with nothing in flight."""
    import time

    import httpx

    from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    class _IdleWS:
        async def send_text(self, data: str) -> None:
            return None

        async def receive_text(self) -> str:
            return await asyncio.Future()

    session_id = "3d7a5c2f9e4b4b1c8f6d8a5e4b2c3d0f"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    monkeypatch.setattr(orchestration, "_RUNNER_STATUS_PROBE_TIMEOUT_S", 0.1)
    registry = TunnelRegistry()
    registry.register(
        "runner_one",
        _IdleWS(),
        HelloFrame(runner_version="0.1.0", frame_protocol_version=1, harnesses=[], envs=[]),
    )
    runner_client = httpx.AsyncClient(
        transport=WSTunnelTransport(registry, "runner_one"), base_url="http://runner"
    )
    _use_runner_client(monkeypatch, runner_client)
    conv_store = _ConversationStore([_message_item("item_1", "hi")])

    try:
        started = time.monotonic()
        snapshot = await _get_session_snapshot(conv_store, session_id)  # type: ignore[arg-type]
        elapsed = time.monotonic() - started
    finally:
        await runner_client.aclose()

    assert snapshot.status == "idle"
    assert elapsed < 1.0
    tunnel = registry.get("runner_one")
    assert tunnel is not None
    assert tunnel.in_flight == {}
    assert _mod._runner_status_probe_backoff.get(session_id) is not None


@pytest.mark.asyncio
async def test_session_snapshot_uses_persisted_status_after_server_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recycled server keeps a native turn running from its durable status.

    Native harness injection returns before the external turn completes, so the
    runner's generic session endpoint can report ``idle`` while Codex is still
    working. After a server restart clears the in-memory status cache, the
    conversation row is the surviving relay truth and must win over that probe.
    """
    from omnigent.server.routes import sessions as _mod

    session_id = "bef42153ba7a4f2cb35350dc23b27c93"
    _mod._session_status_cache.pop(session_id, None)

    class _IdleRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> Any:
            self.get_calls.append(url)
            raise AssertionError("persisted running status must avoid the idle runner probe")

    runner_client = _IdleRunnerClient()
    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: runner_client)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)
    conv = Conversation(
        id=session_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=session_id,
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        live_status="running",
    )

    try:
        snapshot = await _get_session_snapshot(
            _ConversationStore([], conversations={session_id: conv}),  # type: ignore[arg-type]
            session_id,
        )
    finally:
        _mod._session_status_cache.pop(session_id, None)

    assert snapshot.status == "running"
    status_calls = runner_client.get_calls
    assert status_calls == []


@pytest.mark.asyncio
async def test_session_snapshot_defaults_idle_when_runner_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the runner is unreachable on cache miss, status
    defaults to idle rather than crashing."""
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()

    # No runner client available (both router and singleton).
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: None,
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: None,
    )

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    snapshot = await _get_session_snapshot(
        conv_store,
        "68dcdb50d850d9c2b905cad807dad25f",  # type: ignore[arg-type]
    )

    assert snapshot.status == "idle"


@pytest.mark.asyncio
async def test_session_snapshot_uses_router_when_singleton_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router-only deployments (the production shape, plus the
    tunnel-three-layer test fixture) must reach the runner via
    ``get_runner_router()`` on cache miss. Before the fix, the
    cache-miss path only consulted the legacy ``get_runner_client``
    singleton; in any router-only setup that singleton is ``None``,
    so status silently defaulted to ``"idle"`` even when the runner
    had an active turn — which is exactly the cold-start race that
    flaked ``test_native_session_happy_path_via_ws_tunnel``.
    """
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()

    class _FakeResponse:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "running"}

    class _FakeRunnerClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _FakeResponse:
            self.get_calls.append(url)
            return _FakeResponse()

    from omnigent.runner.routing import RoutedRunner

    fake_client = _FakeRunnerClient()

    class _FakeRouter:
        def __init__(self) -> None:
            self.resolved_for: list[str] = []

        def client_for_session_resources(
            self,
            conversation_id: str,
            *,
            conversation: Conversation | None = None,
        ) -> RoutedRunner:
            # Mirrors the real router: callers hand over the row they already
            # authorized so it need not re-read the conversation.
            del conversation
            self.resolved_for.append(conversation_id)
            return RoutedRunner(runner_id="runner_test", client=fake_client)  # type: ignore[arg-type]

    fake_router = _FakeRouter()

    # Singleton stays None (production-shape router-only deployment);
    # router resolves the runner via the conversation's affinity.
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: None,
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: fake_router,
    )

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    snapshot = await _get_session_snapshot(
        conv_store,
        "3ce755917a74a49f0c8feaf50f058ed9",  # type: ignore[arg-type]
    )

    assert fake_router.resolved_for == ["3ce755917a74a49f0c8feaf50f058ed9"], (
        "snapshot should have consulted the runner_router on cache miss "
        "instead of synthesizing a default status"
    )
    assert snapshot.status == "running"
    assert fake_client.get_calls == [
        "/v1/sessions/3ce755917a74a49f0c8feaf50f058ed9",
    ]


@pytest.mark.asyncio
async def test_session_snapshot_prefers_router_over_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both the router and the legacy singleton are wired, the
    router wins — it knows the per-conversation runner affinity, the
    singleton is process-wide and only correct in single-runner mode.
    """
    from omnigent.runner.routing import RoutedRunner
    from omnigent.server.routes import sessions as _mod

    _mod._session_status_cache.clear()

    class _Response:
        def __init__(self, status: str) -> None:
            self.status_code = 200
            self._status = status

        def json(self) -> dict[str, str]:
            return {"status": self._status}

    class _Client:
        def __init__(self, status: str) -> None:
            self._status = status
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 5.0) -> _Response:
            self.get_calls.append(url)
            return _Response(self._status)

    router_client = _Client("running")
    singleton_client = _Client("idle")

    class _FakeRouter:
        def client_for_session_resources(
            self,
            conversation_id: str,
            *,
            conversation: Conversation | None = None,
        ) -> RoutedRunner:
            """Mirrors the real router's already-authorized-row parameter."""
            del conversation_id, conversation
            return RoutedRunner(runner_id="runner_test", client=router_client)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: _FakeRouter(),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_client",
        lambda: singleton_client,
    )

    conv_store = _ConversationStore([_message_item("item_1", "hi")])
    snapshot = await _get_session_snapshot(
        conv_store,
        "1cef6d7d1ef0c577c6ccfc690e5bc8ed",  # type: ignore[arg-type]
    )

    assert snapshot.status == "running"
    assert router_client.get_calls == [
        "/v1/sessions/1cef6d7d1ef0c577c6ccfc690e5bc8ed",
    ]
    assert singleton_client.get_calls == [], (
        "singleton should not have been queried when the router resolved a client"
    )


@dataclass
class _PublishedUsage:
    """One ``session.usage`` event captured from the session stream.

    :param conversation_id: Conversation the event was published to.
    :param event: The serialized ``SessionUsageEvent`` payload.
    """

    conversation_id: str
    event: dict[str, object]


@dataclass
class _UsageStreamRecorder:
    """Captures ``session_stream.publish`` calls for assertions.

    :param published: Every ``(conversation_id, event)`` publish, in order.
    """

    published: list[_PublishedUsage] = field(default_factory=list)

    def publish(self, conversation_id: str, event: dict[str, object]) -> None:
        self.published.append(_PublishedUsage(conversation_id=conversation_id, event=event))


def _graph_conv(
    conv_id: str,
    *,
    root: str,
    parent: str | None,
    cost: float | None,
    tokens: dict[str, float] | None = None,
    by_model: dict[str, dict[str, float]] | None = None,
) -> Conversation:
    """Build a spawn-tree conversation with optional priced ``session_usage``.

    :param conv_id: This conversation's id, e.g. ``"ff5cac23d0beb79fad914046049f32ff"``.
    :param root: Shared spawn-tree root id (every node in a tree shares it).
    :param parent: Parent conversation id, or ``None`` for the tree root.
    :param cost: ``total_cost_usd`` to record, or ``None`` for an unpriced
        conversation (no cost key).
    :param tokens: Per-bucket token counts to record alongside the cost, e.g.
        ``{"input_tokens": 100, "output_tokens": 20}``. ``None`` records no
        token buckets.
    :param by_model: Nested per-model usage to record under ``by_model``, e.g.
        ``{"claude-sonnet-4-6": {"input_tokens": 100, "total_cost_usd": 0.1}}``.
        ``None`` records no per-model breakdown.
    """
    usage: dict[str, Any] = {} if cost is None else {"total_cost_usd": cost}
    if tokens is not None:
        usage.update(tokens)
    if by_model is not None:
        usage["by_model"] = by_model
    return Conversation(
        id=conv_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=root,
        parent_conversation_id=parent,
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        kind="default" if parent is None else "sub_agent",
        session_usage=usage,
    )


@pytest.mark.asyncio
async def test_session_snapshot_cost_sums_subagent_subtree() -> None:
    """A parent's displayed cost includes its sub-agents' spend.

    The snapshot seeds ``total_cost_usd`` from ``load_session_usage`` (the
    subtree sum), not the parent's own ``session_usage``. A sub-agent persists
    its spend on its own child conversation, so without the subtree sum the
    parent's badge would never reflect it.
    """
    parent = _graph_conv(
        "ead6d59a6b650d19dbdf61ec32426f4e",
        root="ead6d59a6b650d19dbdf61ec32426f4e",
        parent=None,
        cost=1.0,
    )
    child = _graph_conv(
        "ff5cac23d0beb79fad914046049f32ff",
        root="ead6d59a6b650d19dbdf61ec32426f4e",
        parent="ead6d59a6b650d19dbdf61ec32426f4e",
        cost=2.5,
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={
            "ead6d59a6b650d19dbdf61ec32426f4e": parent,
            "ff5cac23d0beb79fad914046049f32ff": child,
        },
    )

    snapshot = await _get_session_snapshot(conv_store, "ead6d59a6b650d19dbdf61ec32426f4e")  # type: ignore[arg-type]

    # 3.5 = parent $1.00 + sub-agent $2.50. If this reads 1.00, the snapshot
    # regressed to the parent's own session_usage and dropped the subtree sum —
    # a sub-agent burning budget would be invisible on the parent's badge.
    assert snapshot.total_cost_usd == 3.5


@pytest.mark.asyncio
async def test_session_snapshot_cost_is_own_usage_for_childless_session() -> None:
    """A session with no sub-agents shows exactly its own cost.

    Guards the fallback: a childless session's subtree is just itself, so the
    badge must equal the conversation's own ``total_cost_usd`` (not None/0).
    """
    solo = _graph_conv(
        "6d996c055256e5975b8e5683c4d77d47",
        root="6d996c055256e5975b8e5683c4d77d47",
        parent=None,
        cost=0.42,
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"6d996c055256e5975b8e5683c4d77d47": solo},
    )

    snapshot = await _get_session_snapshot(conv_store, "6d996c055256e5975b8e5683c4d77d47")  # type: ignore[arg-type]

    # 0.42 = the session's own cost; no descendants to add.
    assert snapshot.total_cost_usd == 0.42


@pytest.mark.asyncio
async def test_session_snapshot_sums_by_model_over_subtree() -> None:
    """The snapshot's ``usage_by_model`` sums token buckets across the subtree.

    Mirrors the cost roll-up: each model's per-bucket counts on the parent's
    snapshot must include the sub-agent's tokens, so the per-model breakdown
    reflects the full spawn tree, not just the parent's own turns. When parent
    and child share the same model, their buckets must be summed.
    """
    parent = _graph_conv(
        "ead6d59a6b650d19dbdf61ec32426f4e",
        root="ead6d59a6b650d19dbdf61ec32426f4e",
        parent=None,
        cost=1.0,
        tokens={"input_tokens": 100, "output_tokens": 20},
        by_model={"model-a": {"input_tokens": 100, "output_tokens": 20, "total_cost_usd": 1.0}},
    )
    child = _graph_conv(
        "ff5cac23d0beb79fad914046049f32ff",
        root="ead6d59a6b650d19dbdf61ec32426f4e",
        parent="ead6d59a6b650d19dbdf61ec32426f4e",
        cost=2.5,
        tokens={"input_tokens": 400, "output_tokens": 80},
        by_model={"model-a": {"input_tokens": 400, "output_tokens": 80, "total_cost_usd": 2.5}},
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={
            "ead6d59a6b650d19dbdf61ec32426f4e": parent,
            "ff5cac23d0beb79fad914046049f32ff": child,
        },
    )

    snapshot = await _get_session_snapshot(conv_store, "ead6d59a6b650d19dbdf61ec32426f4e")  # type: ignore[arg-type]

    # The per-model buckets must be parent + child summed.
    assert snapshot.usage_by_model is not None
    assert snapshot.usage_by_model["model-a"].input_tokens == 500
    assert snapshot.usage_by_model["model-a"].output_tokens == 100
    assert snapshot.usage_by_model["model-a"].total_cost_usd == 3.5


@pytest.mark.asyncio
async def test_session_snapshot_usage_by_model_none_when_unrecorded() -> None:
    """An unpriced session with no per-model usage omits ``usage_by_model``.

    ``None`` (no row rendered) rather than an empty dict — an empty dict
    would imply models were tracked but none contributed.
    """
    solo = _graph_conv(
        "6d996c055256e5975b8e5683c4d77d47",
        root="6d996c055256e5975b8e5683c4d77d47",
        parent=None,
        cost=None,
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={"6d996c055256e5975b8e5683c4d77d47": solo},
    )

    snapshot = await _get_session_snapshot(conv_store, "6d996c055256e5975b8e5683c4d77d47")  # type: ignore[arg-type]

    assert snapshot.usage_by_model is None


@pytest.mark.asyncio
async def test_session_snapshot_usage_by_model_merges_differing_models() -> None:
    """The snapshot's ``usage_by_model`` folds in a sub-agent on a different model.

    A parent on ``model-a`` and a sub-agent on ``model-b`` must both appear in
    the parent's per-model breakdown, summed over the subtree and typed as
    :class:`ModelUsage`. Without the subtree merge a supervisor delegating to a
    differently-modeled worker would hide that model's spend.
    """
    parent = _graph_conv(
        "ead6d59a6b650d19dbdf61ec32426f4e",
        root="ead6d59a6b650d19dbdf61ec32426f4e",
        parent=None,
        cost=0.10,
        tokens={"input_tokens": 1000},
        by_model={"model-a": {"input_tokens": 1000, "total_cost_usd": 0.10}},
    )
    child = _graph_conv(
        "ff5cac23d0beb79fad914046049f32ff",
        root="ead6d59a6b650d19dbdf61ec32426f4e",
        parent="ead6d59a6b650d19dbdf61ec32426f4e",
        cost=0.04,
        tokens={"input_tokens": 150},
        by_model={"model-b": {"input_tokens": 150, "total_cost_usd": 0.04}},
    )
    conv_store = _ConversationStore(
        [_message_item("item_1", "hi")],
        conversations={
            "ead6d59a6b650d19dbdf61ec32426f4e": parent,
            "ff5cac23d0beb79fad914046049f32ff": child,
        },
    )

    snapshot = await _get_session_snapshot(conv_store, "ead6d59a6b650d19dbdf61ec32426f4e")  # type: ignore[arg-type]

    assert snapshot.usage_by_model is not None
    # Both models present (typed ModelUsage), each with its own attributed
    # tokens/cost. A missing "model-b" would mean the sub-agent's model was
    # dropped from the parent's per-model view.
    assert snapshot.usage_by_model["model-a"].input_tokens == 1000
    assert snapshot.usage_by_model["model-a"].total_cost_usd == 0.10
    assert snapshot.usage_by_model["model-b"].input_tokens == 150
    assert snapshot.usage_by_model["model-b"].total_cost_usd == 0.04


def test_publish_subtree_cost_to_ancestors_publishes_each_ancestor_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child usage update re-publishes every ancestor's subtree cost.

    A sub-agent's spend lives on its own child conversation, so an ancestor's
    stored usage never moves — without this re-publish a parent's live badge
    would never reflect a running sub-agent. For a grandparent($1) →
    parent($2) → child($4) tree, updating the child must publish
    parent=$6 ({parent, child}) and grandparent=$7 ({all three}), and must NOT
    publish to the originating child.
    """
    g = _graph_conv(
        "8463f762110b86b1ba33ddf7a8fc1172",
        root="8463f762110b86b1ba33ddf7a8fc1172",
        parent=None,
        cost=1.0,
        tokens={"input_tokens": 10},
        by_model={"model-a": {"input_tokens": 10, "total_cost_usd": 1.0}},
    )
    p = _graph_conv(
        "b460374fc8e697b296708f52dc9d8179",
        root="8463f762110b86b1ba33ddf7a8fc1172",
        parent="8463f762110b86b1ba33ddf7a8fc1172",
        cost=2.0,
        tokens={"input_tokens": 20},
        by_model={"model-a": {"input_tokens": 20, "total_cost_usd": 2.0}},
    )
    c = _graph_conv(
        "405bfe154d5c0e795a2b87021bc897bf",
        root="8463f762110b86b1ba33ddf7a8fc1172",
        parent="b460374fc8e697b296708f52dc9d8179",
        cost=4.0,
        tokens={"input_tokens": 40},
        by_model={"model-a": {"input_tokens": 40, "total_cost_usd": 4.0}},
    )
    conv_store = _ConversationStore(
        [],
        conversations={
            "8463f762110b86b1ba33ddf7a8fc1172": g,
            "b460374fc8e697b296708f52dc9d8179": p,
            "405bfe154d5c0e795a2b87021bc897bf": c,
        },
    )
    recorder = _UsageStreamRecorder()
    monkeypatch.setattr(_sessions_mod, "session_stream", recorder)

    _publish_subtree_cost_to_ancestors(conv_store, "405bfe154d5c0e795a2b87021bc897bf")  # type: ignore[arg-type]

    by_conv = {pub.conversation_id: pub.event for pub in recorder.published}
    # Only the two ancestors are re-published — never the originating child.
    # A "conv_c" entry would mean the helper republished the node that already
    # got its own session.usage event (double broadcast); a missing ancestor
    # would mean the parent-to-root walk stopped early.
    assert set(by_conv) == {"b460374fc8e697b296708f52dc9d8179", "8463f762110b86b1ba33ddf7a8fc1172"}
    # parent subtree = parent $2 + child $4. A wrong value means the walk
    # summed the wrong subtree (parent-only, or the whole tree w/ grandparent).
    assert by_conv["b460374fc8e697b296708f52dc9d8179"]["total_cost_usd"] == 6.0
    # grandparent subtree = $1 + $2 + $4 (itself + both descendants).
    assert by_conv["8463f762110b86b1ba33ddf7a8fc1172"]["total_cost_usd"] == 7.0
    # The per-model breakdown rolls up the same subtree alongside the cost.
    assert (
        by_conv["b460374fc8e697b296708f52dc9d8179"]["usage_by_model"]["model-a"]["input_tokens"]
        == 60
    )
    assert (
        by_conv["8463f762110b86b1ba33ddf7a8fc1172"]["usage_by_model"]["model-a"]["input_tokens"]
        == 70
    )
    # The payload is a session.usage broadcast the web client renders as the badge.
    assert by_conv["b460374fc8e697b296708f52dc9d8179"]["type"] == "session.usage"


# ── _truncate_label ──────────────────────────────────────────────────────────


def test_truncate_label_short_value_unchanged() -> None:
    """Values at or below the column limit pass through unmodified."""
    value = "x" * _LABEL_VALUE_MAX_LEN
    assert _truncate_label(value) == value


def test_truncate_label_long_value_fits_column() -> None:
    """Truncated output fits the column, keeps the head, and flags the cut."""
    long_value = "a" * (_LABEL_VALUE_MAX_LEN + 100)
    result = _truncate_label(long_value)
    assert len(result) == _LABEL_VALUE_MAX_LEN
    # The informative head is preserved and a marker signals the truncation.
    assert result.endswith("…")
    assert result[:-1] == long_value[: _LABEL_VALUE_MAX_LEN - 1]


def test_truncate_label_at_limit_no_marker() -> None:
    """A value exactly at the limit is kept verbatim — no spurious ellipsis."""
    value = "b" * _LABEL_VALUE_MAX_LEN
    result = _truncate_label(value)
    assert result == value
    assert not result.endswith("…")


def test_truncate_label_empty_string() -> None:
    """Empty string is returned unchanged (no off-by-one crash)."""
    assert _truncate_label("") == ""


# ── _persist_session_status_error_labels ─────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_error_labels_truncates_long_message() -> None:
    """A failure message longer than 256 chars is truncated before the store
    write, preventing the ``DataError`` that silently dropped the reason."""
    from omnigent.server.schemas import ErrorDetail

    captured: dict[str, dict[str, str]] = {}

    class _MockStore:
        def set_labels(self, session_id: str, updates: dict[str, str]) -> None:
            captured[session_id] = updates

    long_message = "Runner MCP execute failed: " + "x" * 300
    error = ErrorDetail(code="mcp_error", message=long_message)

    await _persist_session_status_error_labels(
        "0099dc8be6d82871e2e450424d46d1b7", error, _MockStore()
    )  # type: ignore[arg-type]

    stored = captured["0099dc8be6d82871e2e450424d46d1b7"]["omnigent.last_task_error_message"]
    assert len(stored) <= _LABEL_VALUE_MAX_LEN
    # The diagnostic prefix survives so the reload-visible reason is still useful.
    assert stored.startswith("Runner MCP execute failed: ")


@pytest.mark.asyncio
async def test_persist_error_labels_short_message_stored_verbatim() -> None:
    """A short failure message is stored without modification."""
    from omnigent.server.schemas import ErrorDetail

    captured: dict[str, dict[str, str]] = {}

    class _MockStore:
        def set_labels(self, session_id: str, updates: dict[str, str]) -> None:
            captured[session_id] = updates

    error = ErrorDetail(code="runner_error", message="Process exited with code 1")

    await _persist_session_status_error_labels(
        "d6e1678fb446a1cf5a892e0df60aaba3", error, _MockStore()
    )  # type: ignore[arg-type]

    assert (
        captured["d6e1678fb446a1cf5a892e0df60aaba3"]["omnigent.last_task_error_message"]
        == "Process exited with code 1"
    )
    assert (
        captured["d6e1678fb446a1cf5a892e0df60aaba3"]["omnigent.last_task_error_code"]
        == "runner_error"
    )


# ── _runner_reject_detail ────────────────────────────────────────────────────


def test_runner_reject_detail_combines_error_code_and_detail() -> None:
    """The runner's own ``{error, detail}`` shape reads as ``code: detail``."""
    import httpx

    from omnigent.server.routes.sessions import _runner_reject_detail

    resp = httpx.Response(
        503,
        request=httpx.Request("POST", "http://runner/v1/sessions/conv_x/events"),
        json={"error": "harness_spawn_failed", "detail": "harness spawn failed (see log)"},
    )
    assert _runner_reject_detail(resp) == "harness_spawn_failed: harness spawn failed (see log)"


def test_runner_reject_detail_falls_back_through_code_body_and_status() -> None:
    """Each degraded body shape still yields a non-empty reason.

    The reason becomes the user-visible ``last_task_error``, so an
    error-code-only body, a non-JSON body, and an empty body must each
    produce something better than a bare "failed".
    """
    import httpx

    from omnigent.server.routes.sessions import _runner_reject_detail

    req = httpx.Request("POST", "http://runner/v1/sessions/conv_x/events")

    code_only = httpx.Response(501, request=req, json={"error": "not_implemented"})
    assert _runner_reject_detail(code_only) == "not_implemented"

    non_json = httpx.Response(400, request=req, text="bad request body")
    assert _runner_reject_detail(non_json) == "bad request body"

    empty = httpx.Response(503, request=req)
    assert _runner_reject_detail(empty) == "runner returned status 503"


def test_runner_reject_detail_tolerates_status_only_response_fake() -> None:
    """A fake exposing only ``status_code`` degrades to the status line.

    Runner-client stubs across the server tests return lightweight fakes
    without ``json()``; the helper must not raise on them.
    """
    from omnigent.server.routes.sessions import _runner_reject_detail

    class _Fake:
        status_code = 503

    assert _runner_reject_detail(_Fake()) == "runner returned status 503"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_persist_error_labels_clears_stale_structured_fields() -> None:
    """An unclassified failure must not inherit a prior failure's title/cause.

    The label store is upsert-only, so every persist writes all structured keys
    (empty when absent). An error with no title/cause/remediation therefore
    projects back to just code + message.
    """
    from omnigent.server.routes.sessions import _last_task_error_from_labels
    from omnigent.server.schemas import ErrorDetail

    captured: dict[str, dict[str, str]] = {}

    class _MockStore:
        def set_labels(self, session_id: str, updates: dict[str, str]) -> None:
            captured[session_id] = updates

    error = ErrorDetail(code="runner_error", message="turn setup failed")
    await _persist_session_status_error_labels(
        "bb22cc33dd44ee55ff66778899001122", error, _MockStore()
    )  # type: ignore[arg-type]

    labels = captured["bb22cc33dd44ee55ff66778899001122"]
    # All structured keys are written empty so a stale value can't leak.
    assert labels["omnigent.last_task_error_title"] == ""
    assert labels["omnigent.last_task_error_cause"] == ""
    assert labels["omnigent.last_task_error_remediation"] == ""
    assert labels["omnigent.last_task_error_agent_name"] == ""
    assert _last_task_error_from_labels(labels) == {
        "code": "runner_error",
        "message": "turn setup failed",
    }


def _side_chat_child(
    *,
    runner_id: str | None,
    parent_id: str | None = "parent",
    nickname: str = "Side chat",
    wrapper: str = "codex-native-ui-subagent",
    kind: str = "sub_agent",
) -> Any:
    """A conversation row shaped like a codex ``/side`` child."""
    labels: dict[str, str] = {"omnigent.wrapper": wrapper}
    if nickname:
        labels["omnigent.codex_native.agent_nickname"] = nickname
    return SimpleNamespace(
        kind=kind,
        labels=labels,
        parent_conversation_id=parent_id,
        runner_id=runner_id,
    )


class _ParentStore:
    """A conversation store that returns one canned parent row."""

    def __init__(self, parent: Any) -> None:
        self._parent = parent

    def get_conversation(self, conversation_id: str) -> Any:
        return self._parent


async def test_snapshot_does_not_query_runner_skills_or_publish_skill_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes import sessions as mod

    mod._session_status_cache.clear()
    calls: list[str] = []
    published: list[dict[str, object]] = []

    class Runner:
        async def get(self, url: str, **kwargs: Any) -> Any:
            calls.append(url)
            assert not url.endswith("/skills")

            class Response:
                status_code = 200

                def json(self) -> dict[str, str]:
                    return {"status": "idle"}

            return Response()

    class Stream:
        @staticmethod
        def publish(session_id: str, event: dict[str, object]) -> None:
            published.append(event)

    monkeypatch.setattr("omnigent.runtime.get_runner_client", lambda: Runner())
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)
    monkeypatch.setattr(mod, "session_stream", Stream)
    snapshot = await _get_session_snapshot(_ConversationStore([]), "session-menu-independent")  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert "skills" not in snapshot.model_dump()
    assert "skills_status" not in snapshot.model_dump()
    assert all(not url.endswith("/skills") for url in calls)
    assert all(event.get("type") != "session.skills" for event in published)


@pytest.mark.asyncio
async def test_status_probe_answer_does_not_overwrite_a_newer_relay_status() -> None:
    """A slow probe that reports ``running`` must not undo the relay's later ``idle``.

    The probe describes the moment the runner answered; the relay's ``idle`` edge arrived
    while it was in flight. Applying the probe would leave the finished turn "Working"
    forever, because no later edge clears it.
    """
    from omnigent.server.routes import sessions as _mod
    from omnigent.server.routes._sessions import orchestration

    session_id = "6a0d8f5c2b3e4e4f9c1a2d0e7f6b8c3d"
    _mod._session_status_cache.pop(session_id, None)
    _mod._runner_status_probe_backoff.pop(session_id, None)
    runner_client = _GatedRunnerClient()

    probe = asyncio.create_task(
        orchestration._probe_runner_live_status(runner_client, session_id)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(runner_client.arrived.wait(), timeout=1.0)
    _mod._session_status_cache[session_id] = "idle"  # the relay's turn-end edge
    runner_client.release.set()

    assert await probe == "idle"
    assert _mod._session_status_cache.get(session_id) == "idle"
