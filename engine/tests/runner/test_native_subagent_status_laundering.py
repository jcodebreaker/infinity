"""Sub-agent dispatch liveness tests.

A dispatch wedged in ``launching`` must fail loudly to the parent instead of
being silently lost, and a started dispatch must not be reaped. The native
terminal-status cases (``external_session_status`` edges) were removed with the
native harnesses (ADR 0020).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from tests.runner.conftest import (
    _FakeProcessManager,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_wedged_launching_dispatch_fails_loudly_to_parent() -> None:
    """
    A dispatch wedged in ``launching`` must fail loudly, never be swallowed.

    User journey: a parent dispatches a native sub-agent turn; the child
    never emits any edge (no running/waiting/terminal status — e.g. the
    native pane never came up, or the send was buffered against a turn that
    never ends). A message buffered against a healthy active turn drains as
    a continuation turn once that turn settles — that path is contractual
    and covered elsewhere — but a child that produces NO edge at all leaves
    the work entry in ``launching`` forever: the parent never hears back and
    the dispatched work is silently lost with no error surfaced anywhere.

    The runner must enforce a launch-liveness budget: a dispatch still in
    ``launching`` past the budget is failed and that failure is delivered to
    the parent inbox, so the send is run-or-failed — never accepted and
    dropped in silence.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = inbox
    entry = runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        # Absent on an unguarded runner: without the sweep, the wedged
        # dispatch simply stays "launching" and the behavioral asserts
        # below observe the silent loss directly.
        reap = getattr(runner_app, "reap_stalled_subagent_launches", None)
        if reap is not None:
            # Within the budget: the dispatch is left alone (still launching).
            assert reap(now=entry.created_at + 10.0, timeout_s=180.0) == []
            assert entry.status == "launching"
            assert inbox.empty()
            # Past the budget with no edge ever seen: the sweep must act.
            reap(now=entry.created_at + 200.0, timeout_s=180.0)

        assert entry.status == "failed", (
            f"work entry is {entry.status!r} long past any reasonable launch "
            f"budget: a child that never produced a single edge is wedged, the "
            f"parent is never told, and the dispatched work is silently lost. "
            f"The runner must fail the dispatch loudly."
        )
        payload = inbox.get_nowait()
        assert payload["status"] == "failed", payload
        assert "no start acknowledgment" in str(payload["output"]), (
            f"the parent must receive an explanatory failure, got {payload['output']!r}"
        )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_started_dispatch_is_not_reaped_by_launch_liveness() -> None:
    """
    A dispatch that produced a real edge must never be launch-reaped.

    Once the child has reported ``running`` (the launch proof), the liveness
    budget no longer applies — a long-running healthy child is not a wedge.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = inbox
    entry = runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        runner_app.mark_subagent_work_started(child_id)
        assert entry.status == "running"
        reaped = runner_app.reap_stalled_subagent_launches(
            now=entry.created_at + 10_000.0, timeout_s=180.0
        )
        assert reaped == []
        assert entry.status == "running"
        assert inbox.empty()
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_reaped_dispatch_wakes_parent_not_just_inbox() -> None:
    """
    A launch-reaped failure must schedule the parent wake POST.

    The wake POST is the sole delivery signal for an idle parent: it only
    drains its inbox when a turn is triggered, and nothing else will rouse a
    parent whose child wedged in ``launching``. A reap that inserts the
    failure into the inbox without the wake leaves the parent hanging exactly
    as before — the failure is recorded where nobody will ever read it.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wake_bodies: list[dict[str, Any]] = []
    wake_seen = asyncio.Event()

    class _WakeRecordingServerClient(NullServerClient):
        """Records wake POSTs aimed at the parent session's events route."""

        async def post(self, url: str, **kwargs: Any) -> Any:
            if url.rstrip("/").endswith(f"/v1/sessions/{parent_id}/events"):
                wake_bodies.append(kwargs.get("json") or {})
                wake_seen.set()
            return self._Response()

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=_WakeRecordingServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = inbox
    entry = runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        reaped = runner_app.reap_stalled_subagent_launches(
            now=entry.created_at + 200.0,
            timeout_s=180.0,
            mark_terminal=app.state.mark_subagent_terminal_and_wake,
        )
        assert reaped == [entry]
        assert entry.status == "failed"
        payload = inbox.get_nowait()
        assert payload["status"] == "failed"

        await asyncio.wait_for(wake_seen.wait(), timeout=5.0)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    notice = wake_bodies[0]["data"]["content"][0]["text"]
    assert "finished (failed)" in notice, (
        f"the reap's wake notice must tell the parent the dispatch failed, got {notice!r}"
    )


def test_launch_timeout_resolver_rejects_non_finite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``nan``/``inf`` overrides must not silently disable the launch reaper.

    ``float("nan")`` makes every ``elapsed < budget`` comparison false and
    ``inf`` makes it always true-to-skip — either silently disables reaping
    without the explicit ``<= 0`` "disabled" intent. Non-finite values are
    rejected like non-numeric ones, falling back to the default.
    """
    from omnigent.runner import app as runner_app

    default = runner_app._DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    for bad in ("nan", "inf", "-inf", "+inf", "bogus"):
        monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", bad)
        assert runner_app.resolve_subagent_launch_timeout_s() == default, bad
    # Explicit disable and a valid override still work.
    monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", "0")
    assert runner_app.resolve_subagent_launch_timeout_s() == 0.0
    monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", "45.5")
    assert runner_app.resolve_subagent_launch_timeout_s() == 45.5
    monkeypatch.delenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S")
    assert runner_app.resolve_subagent_launch_timeout_s() == default
