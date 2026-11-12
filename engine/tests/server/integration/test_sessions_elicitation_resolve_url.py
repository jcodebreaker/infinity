"""
Integration tests for ``POST /v1/sessions/{id}/elicitations/{eid}/resolve``.

This is the dedicated URL endpoint for URL-based elicitation: an
elicitation published in ``mode == "url"`` carries this path as its
``params.url``, and the client hits it directly with the MCP
:class:`ElicitationResult` body instead of POSTing a generic
``approval`` session event. Both paths converge on the shared
``_resolve_elicitation`` helper, so these tests assert the URL
endpoint drives the *same* resolution pipeline:

- Allow / decline round-trips: park a real server-side harness
  Future via the Claude ``PermissionRequest`` hook, resolve it
  through the URL endpoint, and assert the hook returns the matching
  ``decision.behavior``. This proves the URL endpoint resolves the
  ``_harness_elicitation_registry`` Future identically to the event
  path.
- 404 on unknown session and 422 on a malformed verdict — boundary
  validation.
- cross-session guard: a verdict delivered under a
  *different* session must not resolve another session's Future.
- Route-level cross-user guard: a non-owner cannot reach the
  endpoint when auth is active.

Uses the shared ``client`` fixture (real stores + mock LLM) for the
single-user pipeline tests and the auth-enabled ``auth_client`` from
``test_sessions_permissions`` for the cross-user gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime import get_caps, session_stream
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.caps import RuntimeCaps
from omnigent.server.app import create_app
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Policy callable used by public /policies/evaluate coverage ─────


def _ask_for_bash(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that requires human approval for Bash tool calls.

    :param event: V0 policy event dict.
    :returns: ASK for Bash tool calls, ALLOW otherwise.
    """
    if event.get("type") != "tool_call":
        return {"result": "ALLOW"}
    data = event.get("data")
    tool = data.get("name", "") if isinstance(data, dict) else ""
    if tool == "Bash":
        return {"result": "ASK", "reason": "Approve child tool call"}
    return {"result": "ALLOW"}


# ── Auth-enabled fixtures (per-module, matching the convention in
#    test_sessions_permissions.py — these are redefined per test
#    module rather than promoted to conftest) ──────────────────


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """
    App fixture with a permission store + auth provider enabled.

    Mirrors the shared ``app`` fixture from ``conftest.py`` but adds
    a :class:`SqlAlchemyPermissionStore` and an auth provider so
    access-control checks are live on the session routes — required
    to exercise the cross-user gate on the resolve endpoint.

    :param runtime_init: Fixture that initializes the runtime with a
        mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """
    HTTP client wired to the auth-enabled app.

    Same lifecycle as the shared ``client`` fixture: starts the
    harness process manager, yields the client, then tears down.

    :param auth_app: The auth-enabled FastAPI app.
    :param mock_llm: Controllable mock LLM (released on teardown).
    :param tmp_path: Pytest temporary directory fixture.
    """
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    user: str | None = None,
) -> str:
    """
    Create a minimal session and return its id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :param user: Optional ``X-Forwarded-Email`` identity for
        auth-enabled apps, e.g. ``"alice@example.com"``. ``None``
        sends no header (single-user / no-auth setups).
    :returns: New session id.
    """
    headers = {"X-Forwarded-Email": user} if user is not None else None
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        headers=headers,
    )
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


def _create_child_session(
    db_uri: str,
    *,
    parent_id: str,
    agent_id: str,
    title: str = "codex-child:approval",
) -> str:
    """
    Create a child conversation under a parent session.

    :param db_uri: Test database URI.
    :param parent_id: Parent session id, e.g. ``"ead6d59a6b650d19dbdf61ec32426f4e"``.
    :param agent_id: Agent id inherited by the child.
    :param title: Sub-agent title in ``"<agent>:<name>"`` form, e.g.
        ``"codex-child:approval"``. Must be unique per parent — the DB
        enforces ``(parent_conversation_id, title)`` uniqueness, so a
        fan-out test creating multiple children under one parent must
        pass distinct titles.
    :returns: Child session id, e.g. ``"ff5cac23d0beb79fad914046049f32ff"``.
    """
    store = SqlAlchemyConversationStore(db_uri)
    child = store.create_conversation(
        kind="sub_agent",
        title=title,
        parent_conversation_id=parent_id,
        agent_id=agent_id,
    )
    return child.id


def _tool_call_request(
    tool_name: str = "Bash",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a ``PHASE_TOOL_CALL`` policy-evaluate request.

    :param tool_name: Tool name, e.g. ``"Bash"``.
    :param arguments: Tool arguments dict. ``None`` means no args.
    :returns: JSON body for ``POST /policies/evaluate``.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {
                "name": tool_name,
                "arguments": arguments or {},
            },
            "context": {},
        },
    }


def _patch_default_policies(monkeypatch: pytest.MonkeyPatch, fn_path: str) -> None:
    """
    Install one function policy as the runtime default policy.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param fn_path: Dotted path to the policy callable, e.g.
        ``"tests.server.integration.test_sessions_elicitation_resolve_url._ask_for_bash"``.
    :returns: None.
    """
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[
            FunctionPolicySpec(
                name="admin__ask_bash",
                on=None,
                function=FunctionRef(path=fn_path),
            )
        ],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )


async def _drain_until_elicitation(
    session_id: str,
    *,
    subscribed: asyncio.Event | None = None,
    timeout_s: float = 5.0,
) -> str:
    """
    Block on the session SSE stream until a
    ``response.elicitation_request`` event arrives; return its id.

    The permission hook publishes the SSE event before parking on
    the Future, so subscribing is the simplest way to learn the
    generated id without monkey-patching ``secrets``.

    :param session_id: Session to subscribe to.
    :param subscribed: Optional event to set once the stream
        subscriber is registered.
    :param timeout_s: Max seconds to wait before failing the test.
    :returns: The published ``elicitation_id``.
    """
    event = await _drain_until_elicitation_event(
        session_id,
        subscribed=subscribed,
        timeout_s=timeout_s,
    )
    elicitation_id = event.get("elicitation_id")
    assert isinstance(elicitation_id, str) and elicitation_id, (
        f"elicitation event missing id: {event!r}"
    )
    return elicitation_id


async def _drain_until_elicitation_event(
    session_id: str,
    *,
    subscribed: asyncio.Event | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Block on a session stream until an elicitation event arrives.

    :param session_id: Session to subscribe to.
    :param subscribed: Optional event to set once the stream
        subscriber is registered, e.g. before publishing a prompt
        that would otherwise be dropped by the live-tail stream.
    :param timeout_s: Max seconds to wait before failing the test.
    :returns: The full ``response.elicitation_request`` event.
    """

    async def _on_subscribed() -> tuple[dict[str, Any], ...]:
        """
        Signal that the live-tail subscriber has been registered.

        :returns: Empty snapshot event tuple.
        """
        if subscribed is not None:
            subscribed.set()
        return ()

    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(
            session_id,
            on_subscribed=_on_subscribed,
        ):
            if event.get("type") == "response.elicitation_request":
                return event
    raise AssertionError("subscribe loop ended without an elicitation event")


async def _drain_until_elicitation_resolved(
    session_id: str,
    elicitation_id: str,
    *,
    subscribed: asyncio.Event | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Block on a session stream until one elicitation resolves.

    :param session_id: Session to subscribe to.
    :param elicitation_id: Elicitation id to wait for, e.g.
        ``"elicit_abc123"``.
    :param subscribed: Optional event to set once the stream
        subscriber is registered.
    :param timeout_s: Max seconds to wait before failing the test.
    :returns: The full ``response.elicitation_resolved`` event.
    """

    async def _on_subscribed() -> tuple[dict[str, Any], ...]:
        """
        Signal that the live-tail subscriber has been registered.

        :returns: Empty snapshot event tuple.
        """
        if subscribed is not None:
            subscribed.set()
        return ()

    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(
            session_id,
            on_subscribed=_on_subscribed,
        ):
            if (
                event.get("type") == "response.elicitation_resolved"
                and event.get("elicitation_id") == elicitation_id
            ):
                return event
    raise AssertionError(f"subscribe loop ended without resolved event for {elicitation_id}")


def _claude_permission_payload(tool_name: str = "Bash") -> dict[str, Any]:
    """
    Build a realistic Claude ``PermissionRequest`` hook body.

    :param tool_name: Tool Claude wants to call, e.g. ``"Bash"``.
    :returns: JSON-serializable payload mirroring Claude Code's
        published wire shape for the ``PermissionRequest`` event.
        Deliberately carries no ``tool_use_id``: the real
        PermissionRequest payload has no per-call id (it is minted only
        when the tool call is emitted, after this permission check).
    """
    return {
        "session_id": "claude_sess_abc",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/cwd",
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": tool_name,
        "tool_input": {"command": "ls -la"},
    }


def _claude_ask_user_question_payload() -> dict[str, Any]:
    """
    Build a realistic Claude ``PermissionRequest`` body for AskUserQuestion.

    Mirrors the wire shape Claude Code sends when its built-in
    ``AskUserQuestion`` tool needs permission: ``tool_input`` carries the
    full ``questions`` list (each with ``options``), which the server
    projects into the structured ``params.ask_user_question`` extra. Used
    to prove a sub-agent's interactive question card mirrors to the parent.

    :returns: JSON-serializable PermissionRequest payload whose
        ``tool_name`` is ``"AskUserQuestion"``.
    """
    return {
        "session_id": "claude_sess_ask",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/cwd",
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "Which database should the worker target?",
                    "header": "Database",
                    "multiSelect": False,
                    "options": [
                        {"label": "postgres", "description": "Primary store"},
                        {"label": "sqlite", "description": "Local dev"},
                    ],
                },
            ],
        },
    }


async def test_child_policy_elicitation_bubbles_to_parent_stream(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A policy ASK under a child is visible and actionable from parent chat.

    The public ``POST /policies/evaluate`` route parks TOOL_CALL ASK
    results behind an elicitation Future. When that route runs for a
    child session, a parent stream subscriber receives the prompt with
    ``target_session_id`` and can resolve the child's gate.

    :param client: The test HTTP client.
    :param db_uri: Test database URI.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runtime import pending_elicitations

    _patch_default_policies(monkeypatch, f"{__name__}._ask_for_bash")
    agent = await create_test_agent(client, "test-child-policy-bubble")
    parent_id = await _create_session(client, agent["id"])
    child_id = _create_child_session(db_uri, parent_id=parent_id, agent_id=agent["id"])
    parent_subscribed = asyncio.Event()
    parent_drain = asyncio.create_task(
        _drain_until_elicitation_event(parent_id, subscribed=parent_subscribed)
    )
    await parent_subscribed.wait()
    evaluate = asyncio.create_task(
        client.post(
            f"/v1/sessions/{child_id}/policies/evaluate",
            json=_tool_call_request("Bash", {"command": "date"}),
        )
    )
    parent_resolved: asyncio.Task[dict[str, Any]] | None = None

    try:
        event = await parent_drain
        elicitation_id = event.get("elicitation_id")
        assert isinstance(elicitation_id, str) and elicitation_id
        assert event["elicitation_id"] == elicitation_id
        assert event["params"]["target_session_id"] == child_id
        assert event["params"]["message"] == "admin__ask_bash: Approve child tool call"
        assert event["params"]["phase"] == "tool_call"

        parent_resolved_subscribed = asyncio.Event()
        parent_resolved = asyncio.create_task(
            _drain_until_elicitation_resolved(
                parent_id,
                elicitation_id,
                subscribed=parent_resolved_subscribed,
            )
        )
        await parent_resolved_subscribed.wait()

        verdict = await client.post(
            f"/v1/sessions/{child_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text

        resolved_event = await parent_resolved
        assert resolved_event == {
            "type": "response.elicitation_resolved",
            "elicitation_id": elicitation_id,
            "action": "accept",
        }

        resp = await evaluate
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == "POLICY_ACTION_ALLOW"
    finally:
        if not evaluate.done():
            evaluate.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await evaluate
        parent_drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await parent_drain
        if parent_resolved is not None and not parent_resolved.done():
            parent_resolved.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await parent_resolved
        pending_elicitations.reset_for_tests()


async def test_decline_verdict_rides_resolved_events_to_parent(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A declined child gate publishes ``action: "decline"`` on the resolve fan-out.

    Regression for the fabricated-approval transcript bug: resolving a
    parked sub-agent approval with ``action == "decline"`` via the
    generic approval event (the exact POST a web card sends) must carry
    the verdict on both the session's and every ancestor's resolved
    event. A bare "resolved" left parent agents narrating an approval
    that did not happen.
    """
    from omnigent.runtime import pending_elicitations

    _patch_default_policies(monkeypatch, f"{__name__}._ask_for_bash")
    agent = await create_test_agent(client, "test-child-decline-verdict")
    parent_id = await _create_session(client, agent["id"])
    child_id = _create_child_session(db_uri, parent_id=parent_id, agent_id=agent["id"])
    parent_subscribed = asyncio.Event()
    parent_drain = asyncio.create_task(
        _drain_until_elicitation_event(parent_id, subscribed=parent_subscribed)
    )
    await parent_subscribed.wait()
    evaluate = asyncio.create_task(
        client.post(
            f"/v1/sessions/{child_id}/policies/evaluate",
            json=_tool_call_request("Bash", {"command": "date"}),
        )
    )
    child_resolved: asyncio.Task[dict[str, Any]] | None = None
    parent_resolved: asyncio.Task[dict[str, Any]] | None = None

    try:
        event = await parent_drain
        elicitation_id = event.get("elicitation_id")
        assert isinstance(elicitation_id, str) and elicitation_id

        child_subscribed = asyncio.Event()
        child_resolved = asyncio.create_task(
            _drain_until_elicitation_resolved(
                child_id,
                elicitation_id,
                subscribed=child_subscribed,
            )
        )
        await child_subscribed.wait()
        parent_resolved_subscribed = asyncio.Event()
        parent_resolved = asyncio.create_task(
            _drain_until_elicitation_resolved(
                parent_id,
                elicitation_id,
                subscribed=parent_resolved_subscribed,
            )
        )
        await parent_resolved_subscribed.wait()

        # The exact POST shape from the repro: generic approval event,
        # payload nested under ``data``, action decline.
        verdict = await client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "approval",
                "data": {"elicitation_id": elicitation_id, "action": "decline"},
            },
        )
        assert verdict.status_code == 202, verdict.text

        assert await child_resolved == {
            "type": "response.elicitation_resolved",
            "elicitation_id": elicitation_id,
            "action": "decline",
        }
        assert await parent_resolved == {
            "type": "response.elicitation_resolved",
            "elicitation_id": elicitation_id,
            "action": "decline",
        }

        resp = await evaluate
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == "POLICY_ACTION_DENY"
    finally:
        if not evaluate.done():
            evaluate.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await evaluate
        for task in (child_resolved, parent_resolved):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        pending_elicitations.reset_for_tests()


async def test_child_mcp_elicitation_bubbles_to_parent_stream(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child MCP ``elicitation/create`` prompt is actionable from parent chat.

    When a sub-agent calls a tool whose MCP server sends
    ``elicitation/create``, the runner relays it to AP as a
    ``mcp_elicitation`` event on the child session. The parent
    (Nessie) chat must receive the mirrored prompt with
    ``params.target_session_id`` so the user can answer it there, and
    the resolved signal must mirror back up when the verdict lands —
    otherwise the parent's card never appears (or never clears) for an
    MCP-driven sub-agent prompt. This guards the
    ``_MCP_ELICITATION_TYPE`` branch, which is a separate
    elicitation-origination path from the policy / Codex / Claude
    hooks covered above.

    :param client: The test HTTP client.
    :param db_uri: Test database URI.
    """
    from omnigent.runtime import pending_elicitations

    agent = await create_test_agent(client, "test-child-mcp-bubble")
    parent_id = await _create_session(client, agent["id"])
    child_id = _create_child_session(db_uri, parent_id=parent_id, agent_id=agent["id"])

    parent_subscribed = asyncio.Event()
    parent_drain = asyncio.create_task(
        _drain_until_elicitation_event(parent_id, subscribed=parent_subscribed)
    )
    parent_resolved: asyncio.Task[dict[str, Any]] | None = None
    try:
        await parent_subscribed.wait()
        # The runner posts the MCP elicitation as a control event on
        # the child; it resolves synchronously and returns the minted id.
        publish = await client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "mcp_elicitation",
                "data": {
                    "message": "MCP server asks: proceed with deletion?",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"confirm": {"type": "boolean"}},
                    },
                },
            },
        )
        # The events route acknowledges control events with 202.
        assert publish.status_code == 202, publish.text
        elicitation_id = publish.json()["elicitation_id"]
        assert isinstance(elicitation_id, str) and elicitation_id

        event = await parent_drain
        assert event["type"] == "response.elicitation_request"
        assert event["elicitation_id"] == elicitation_id
        # The mirrored card carries the child id so the parent UI posts
        # the verdict to the child's resolve endpoint, not the parent's.
        assert event["params"]["target_session_id"] == child_id
        assert event["params"]["message"] == "MCP server asks: proceed with deletion?"

        parent_resolved_subscribed = asyncio.Event()
        parent_resolved = asyncio.create_task(
            _drain_until_elicitation_resolved(
                parent_id,
                elicitation_id,
                subscribed=parent_resolved_subscribed,
            )
        )
        await parent_resolved_subscribed.wait()

        # Resolve via the child session (what the parent UI does with the
        # mirrored target_session_id). The MCP path resolves through the
        # generic approval event → _resolve_elicitation, which mirrors the
        # resolved signal back up to ancestors.
        verdict = await client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "approval",
                "data": {"elicitation_id": elicitation_id, "action": "accept"},
            },
        )
        assert verdict.status_code == 202, verdict.text

        resolved_event = await parent_resolved
        assert resolved_event == {
            "type": "response.elicitation_resolved",
            "elicitation_id": elicitation_id,
            "action": "accept",
        }
    finally:
        for task in [parent_drain, parent_resolved]:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        pending_elicitations.reset_for_tests()


class _InputRequiredRunnerClient:
    """
    Runner-client stub for the MCP proxy MRTR loop.

    The first ``/mcp/execute`` POST returns an ``input_required``
    result (the external MCP server wants user input); the retry
    carrying ``inputResponses`` returns a successful tool output.
    Raises on any third execute — the MRTR loop must make exactly
    two. Posts to other runner paths (e.g. the approval-event
    forward fired by the resolve endpoint) are acknowledged with an
    empty success body and not counted as executes.

    :param mcp_eid: The MCP server's elicitation id key inside
        ``inputRequests``, e.g. ``"mcp_eid_1"``.
    """

    def __init__(self, mcp_eid: str = "mcp_eid_1") -> None:
        self._mcp_eid = mcp_eid
        self.calls: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float,
    ) -> httpx.Response:
        """
        Record the execute payload and return the scripted response.

        :param url: Runner path, e.g.
            ``"/v1/sessions/ff5cac23d0beb79fad914046049f32ff/mcp/execute"``.
        :param json: The request body.
        :param timeout: Forward timeout (ignored by the stub).
        :returns: A real ``httpx.Response`` with the scripted JSON.
        """
        if not url.endswith("/mcp/execute"):
            return httpx.Response(200, json={}, request=httpx.Request("POST", url))
        self.calls.append(json)
        if len(self.calls) == 1:
            payload: dict[str, Any] = {
                "result": {
                    "input_required": {
                        "requestState": "mrtr_state_1",
                        "inputRequests": {
                            self._mcp_eid: {
                                "params": {
                                    "mode": "form",
                                    "message": "MCP server asks: overwrite file?",
                                    "requestedSchema": {
                                        "type": "object",
                                        "properties": {"confirm": {"type": "boolean"}},
                                    },
                                },
                            },
                        },
                    },
                },
            }
        elif len(self.calls) == 2:
            payload = {"result": {"output": "tool ran"}}
        else:
            raise AssertionError(
                "runner execute called more than twice — the MRTR loop "
                "should be exactly one input_required round plus one retry"
            )
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))


async def test_child_mcp_input_required_bubbles_to_parent_stream(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A child's runner-proxied MCP ``input_required`` prompt is actionable
    from the parent chat.

    The MCP proxy route parks on
    ``_publish_and_wait_for_harness_elicitation`` when the runner
    reports an external MCP server's ``InputRequiredResult``. This is a
    separate origination path from the ``mcp_elicitation`` control
    event covered above — a missed ``conversation_store`` wire here
    means the prompt publishes only on the child and a Nessie parent
    chat never shows the card. Asserts the mirrored request carries
    ``target_session_id``, the resolved signal mirrors back up, and the
    accepted verdict reaches the runner retry as ``inputResponses``.

    :param client: The test HTTP client.
    :param db_uri: Test database URI.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runtime import pending_elicitations

    agent = await create_test_agent(client, "test-child-mrtr-bubble")
    parent_id = await _create_session(client, agent["id"])
    child_id = _create_child_session(db_uri, parent_id=parent_id, agent_id=agent["id"])

    runner_stub = _InputRequiredRunnerClient()

    async def _fake_get_runner_client(session_id: str, runner_router: Any) -> Any:
        """
        Hand the route the stub in place of a WS-tunnel runner client.

        :param session_id: Session the route wants a runner for.
        :param runner_router: Ignored.
        :returns: The scripted stub client.
        """
        return runner_stub

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _fake_get_runner_client,
    )

    mcp_task: asyncio.Task[httpx.Response] | None = None
    parent_resolved: asyncio.Task[dict[str, Any]] | None = None
    try:
        subscribed = asyncio.Event()
        parent_drain = asyncio.create_task(
            _drain_until_elicitation_event(parent_id, subscribed=subscribed)
        )
        await subscribed.wait()
        mcp_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{child_id}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "mcp__test__echo", "arguments": {}},
                },
            )
        )
        event = await parent_drain
        # The mirrored card carries the child id so the parent UI posts
        # the verdict to the child's resolve endpoint, not the parent's.
        assert event["params"]["target_session_id"] == child_id
        assert event["params"]["message"] == "MCP server asks: overwrite file?"
        elicitation_id = event["elicitation_id"]

        parent_resolved_subscribed = asyncio.Event()
        parent_resolved = asyncio.create_task(
            _drain_until_elicitation_resolved(
                parent_id,
                elicitation_id,
                subscribed=parent_resolved_subscribed,
            )
        )
        await parent_resolved_subscribed.wait()

        verdict = await client.post(
            f"/v1/sessions/{child_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text

        # The ancestor card must clear when the child resolves —
        # otherwise the parent chat shows a zombie approval forever.
        resolved_event = await parent_resolved
        assert resolved_event["elicitation_id"] == elicitation_id

        resp = await mcp_task
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The retry's output survives the round-trip; an error here
        # means the verdict never reached the runner retry.
        assert "error" not in body, f"MCP proxy returned error: {body}"
        assert body["result"]["content"] == [{"type": "text", "text": "tool ran"}]
        # The accept verdict (not a decline default) reached the runner.
        assert runner_stub.calls[1]["params"]["inputResponses"] == {
            "mcp_eid_1": {"action": "accept"},
        }
        assert runner_stub.calls[1]["params"]["requestState"] == "mrtr_state_1"
    finally:
        for task in [mcp_task, parent_resolved]:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        pending_elicitations.reset_for_tests()


async def test_resolve_url_unknown_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """
    Resolving against a session that does not exist returns 404.

    The endpoint loads the conversation before resolving so a bad
    URL fails loud rather than silently no-op'ing.
    """
    resp = await client.post(
        "/v1/sessions/1d0b12236c77f69f5073a53583de1a3f/elicitations/elicit_nope/resolve",
        json={"action": "accept"},
    )
    assert resp.status_code == 404, resp.text


async def test_resolve_url_rejects_invalid_action(client: httpx.AsyncClient) -> None:
    """
    A body whose ``action`` is not an MCP literal is rejected at the
    boundary with 422.

    The endpoint types its body as :class:`ElicitationResult`, so
    FastAPI validates the MCP ``action`` enum before any resolution
    runs — a typo can't be silently treated as a non-accept.
    """
    agent = await create_test_agent(client, "test-resolve-url-bad-action")
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/elicitations/elicit_anything/resolve",
        json={"action": "approve"},  # not in {"accept","decline","cancel"}
    )
    assert resp.status_code == 422, resp.text


async def test_resolve_url_cross_user_forbidden(
    auth_client: httpx.AsyncClient,
) -> None:
    """
    A non-owner cannot reach the resolve endpoint when auth is
    active.

    Alice owns the session; Bob's POST to its resolve URL is rejected
    by the ``LEVEL_EDIT`` access gate before any resolution runs. The
    unguessable elicitation id is a capability, but session-owner
    access control is the outer fence — Bob must not get past it even
    with a valid-looking id.
    """
    agent = await create_test_agent(auth_client, user="alice@example.com")
    session_id = await _create_session(auth_client, agent["id"], user="alice@example.com")

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/elicitations/elicit_whatever/resolve",
        json={"action": "accept"},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    # Non-owner is denied (403 forbidden, or 404 to avoid leaking
    # existence — both are acceptable refusals).
    assert resp.status_code in (403, 404), resp.text


# ── GET /sessions/{id}/elicitations/{eid} (approval page) ────


async def test_elicitation_page_returns_resolved_json(
    client: httpx.AsyncClient,
) -> None:
    """
    When the elicitation has already been resolved (or the id is unknown),
    the endpoint returns ``status: "resolved"`` with no policy fields.
    """
    agent = await create_test_agent(client, "test-elicit-page-resolved")
    session_id = await _create_session(client, agent["id"])

    resp = await client.get(
        f"/v1/sessions/{session_id}/elicitations/elicit_unknown_id",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "resolved"
    assert "message" not in data


async def test_elicitation_page_unknown_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """
    Requesting the page for a nonexistent session returns 404.
    """
    resp = await client.get(
        "/v1/sessions/1d0b12236c77f69f5073a53583de1a3f/elicitations/elicit_nope",
    )
    assert resp.status_code == 404, resp.text


async def test_elicitation_page_cross_user_forbidden(
    auth_client: httpx.AsyncClient,
) -> None:
    """
    A non-owner cannot view the approval page when auth is active.
    """
    agent = await create_test_agent(auth_client, user="alice@example.com")
    session_id = await _create_session(auth_client, agent["id"], user="alice@example.com")

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/elicitations/elicit_whatever",
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert resp.status_code in (403, 404), resp.text


# ── _mcp_input_required_response URL mode ─────────────


def test_mrtr_response_url_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    When ``_ELICITATION_MODE`` is ``"url"`` and ``session_id`` is
    provided, the MRTR ``InputRequiredResult`` params carry
    ``mode: "url"`` and the approval page path.
    """

    monkeypatch.setattr("omnigent.server.routes.sessions._ELICITATION_MODE", "url")

    from omnigent.server.routes.sessions import _mcp_input_required_response

    resp = _mcp_input_required_response(
        rpc_id=1,
        elicitation_id="elicit_abc",
        message="Approve?",
        request_state='{"elicitation_id":"elicit_abc","session_id":"0099dc8be6d82871e2e450424d46d1b7"}',
        session_id="0099dc8be6d82871e2e450424d46d1b7",
    )
    body = json.loads(resp.body)
    params = body["result"]["inputRequests"]["elicit_abc"]["params"]
    assert params["mode"] == "url"
    assert params["url"] == "/approve/0099dc8be6d82871e2e450424d46d1b7/elicit_abc"
    assert params["message"] == "Approve?"


def test_mrtr_response_form_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    When ``_ELICITATION_MODE`` is ``"form"``, the MRTR response stays
    in form mode with no ``url`` field.
    """

    monkeypatch.setattr("omnigent.server.routes.sessions._ELICITATION_MODE", "form")

    from omnigent.server.routes.sessions import _mcp_input_required_response

    resp = _mcp_input_required_response(
        rpc_id=1,
        elicitation_id="elicit_abc",
        message="Approve?",
        request_state="{}",
        session_id="0099dc8be6d82871e2e450424d46d1b7",
    )
    body = json.loads(resp.body)
    params = body["result"]["inputRequests"]["elicit_abc"]["params"]
    assert params["mode"] == "form"
    assert "url" not in params


def test_mrtr_response_no_session_id_stays_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Without ``session_id``, the MRTR response uses form mode regardless
    of the elicitation mode config.
    """

    monkeypatch.setattr("omnigent.server.routes.sessions._ELICITATION_MODE", "url")

    from omnigent.server.routes.sessions import _mcp_input_required_response

    resp = _mcp_input_required_response(
        rpc_id=1,
        elicitation_id="elicit_abc",
        message="Approve?",
        request_state="{}",
    )
    body = json.loads(resp.body)
    params = body["result"]["inputRequests"]["elicit_abc"]["params"]
    assert params["mode"] == "form"
    assert "url" not in params
