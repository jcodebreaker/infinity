"""
A large tool result crosses the ``/mcp`` proxy without stalling the loop (ADR 0026).

A deck build returns about 1 MB. The handler decodes the runner's response
and encodes its own JSON-RPC reply; above 64 KB both must happen on a worker
thread so ``/health`` and every other session keep answering.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes._sessions import helpers as helpers_mod
from omnigent.server.routes._sessions import orchestration as orchestration_mod
from omnigent.server.routes.sessions import _handle_mcp_tools_call
from omnigent.spec.types import Phase, PolicyAction

_SESSION_ID = "conv_mcp_large"
_LARGE = 64 * 1024


class _ConversationStore:
    def get_conversation(self, session_id: str) -> Conversation | None:
        if session_id != _SESSION_ID:
            return None
        return Conversation(
            id=_SESSION_ID,
            created_at=0,
            updated_at=0,
            root_conversation_id=_SESSION_ID,
            agent_id="agent_mcp_large",
        )


class _AllowPolicyEngine:
    def __init__(self) -> None:
        self.phases: list[Phase] = []

    async def evaluate(self, ctx: EvaluationContext) -> PolicyResult:
        self.phases.append(ctx.phase)
        return PolicyResult(action=PolicyAction.ALLOW)

    def apply_label_writes(self, set_labels: dict[str, str]) -> None:
        del set_labels


class _LoopGatedJson:
    """
    ``json`` stand-in whose large ``loads``/``dumps`` wait for a loop heartbeat.

    The heartbeat is set only by a task on the event loop. A large call made
    on the loop thread would wait for a heartbeat that can never come.
    """

    def __init__(self) -> None:
        self.heartbeat = threading.Event()
        self.large_calls: list[str] = []
        self.JSONDecodeError = json.JSONDecodeError

    def _wait_for_loop(self, op: str) -> None:
        self.large_calls.append(op)
        self.heartbeat.clear()
        if not self.heartbeat.wait(timeout=5):
            raise AssertionError(f"json.{op} of a large payload ran on the event loop")

    def loads(self, raw: str | bytes, **kwargs: Any) -> Any:
        if len(raw) > _LARGE:
            self._wait_for_loop("loads")
        return json.loads(raw, **kwargs)

    def dumps(self, value: Any, **kwargs: Any) -> str:
        text = json.dumps(value, **kwargs)
        if len(text) > _LARGE:
            self._wait_for_loop("dumps")
        return text


class _Response:
    """An httpx-like response whose ``json()`` decodes through *gate* like httpx would."""

    def __init__(self, body: bytes, gate: _LoopGatedJson) -> None:
        self.content = body
        self._gate = gate

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._gate.loads(self.content)


class _RunnerClient:
    def __init__(self, body: bytes, gate: _LoopGatedJson) -> None:
        self._body = body
        self._gate = gate

    async def post(self, *_args: object, **_kwargs: object) -> _Response:
        return _Response(self._body, self._gate)


async def _beat(gate: _LoopGatedJson, stop: asyncio.Event) -> None:
    while not stop.is_set():
        gate.heartbeat.set()
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_one_megabyte_result_round_trips_with_the_loop_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "x" * (1024 * 1024)
    runner_body = json.dumps({"result": {"output": output}}).encode()
    engine = _AllowPolicyEngine()
    gate = _LoopGatedJson()
    monkeypatch.setattr(orchestration_mod, "json", gate)
    monkeypatch.setattr(helpers_mod, "json", gate)
    monkeypatch.setattr(
        sessions_mod,
        "_load_agent_spec_for_session",
        lambda conv, agent_store: SimpleNamespace(),
    )
    monkeypatch.setattr(
        sessions_mod,
        "_build_policy_engine_from_spec",
        lambda spec, session_id, conversation_store, conversation=None: engine,
    )

    async def _runner_client(session_id: str, runner_router: Any) -> Any:
        del session_id, runner_router
        return _RunnerClient(runner_body, gate)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _runner_client)

    stop = asyncio.Event()
    beat = asyncio.create_task(_beat(gate, stop))
    try:
        response = await _handle_mcp_tools_call(
            rpc_id=3,
            session_id=_SESSION_ID,
            params={"name": "deck_build", "arguments": {}},
            conversation_store=_ConversationStore(),  # type: ignore[arg-type]
            agent_store=object(),  # type: ignore[arg-type]
            runner_router=object(),  # type: ignore[arg-type]
        )
    finally:
        stop.set()
        await beat

    payload = json.loads(bytes(response.body))
    assert payload["id"] == 3
    assert "error" not in payload, payload["error"]
    assert payload["result"] == {"content": [{"type": "text", "text": output}]}
    assert gate.large_calls == ["loads", "dumps"]
    assert engine.phases == [Phase.TOOL_CALL, Phase.TOOL_RESULT]


def test_arguments_preview_matches_a_full_dump_prefix() -> None:
    """The ASK preview cuts large strings first and still equals the full dump's prefix."""
    arguments = {"path": "deck.html", "body": 'é"<' * 100_000, "n": 3, "nested": {"a": [1]}}
    assert orchestration_mod._arguments_preview(arguments) == json.dumps(arguments)[:1024]
    small = {"a": 1}
    assert orchestration_mod._arguments_preview(small) == json.dumps(small)
