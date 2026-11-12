"""A Stop cancels the tool the turn is running (ADR 0026 "Stop stops")."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner import tool_dispatch as _tool_dispatch
from omnigent.runner.mcp_execution_registry import (
    STOPPED_RESULT,
    McpExecutionRegistry,
    McpExecutionResult,
)
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

SESSION = "5b0f6f3c1d2a4e5f8a9b0c1d2e3f4a5b"


class _OpenTurnHarness(_ScriptedHarnessClient):
    """A harness whose turn stays open until the runner cancels it."""

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        self.posted_bodies.append(json)

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                yield _sse({"type": "response.created", "response": {"id": "resp_open"}})
                await asyncio.Event().wait()

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> _Handle:
                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Ctx()


@pytest.mark.asyncio
async def test_interrupt_cancels_a_running_tool_and_ends_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()

    async def fake_execute_tool(**kwargs: Any) -> str:
        del kwargs
        tool_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise
        return "{}"

    monkeypatch.setattr(_tool_dispatch, "execute_tool", fake_execute_tool)
    # The cancelled turn logs its traceback; rendering it in the test console blocks the loop.
    monkeypatch.setattr(logging.getLogger("omnigent.runner.app"), "disabled", True)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1)

    harness = _OpenTurnHarness([])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    transport = httpx.ASGITransport(app=app, client=("tunnel", 0))
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        seed = await client.post(
            f"/v1/sessions/{SESSION}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "probe-agent",
                "content": [{"type": "input_text", "text": "work"}],
                "harness": "claude-sdk",
            },
        )
        assert seed.status_code == 202
        for _ in range(100):
            if harness.posted_bodies:
                break
            await asyncio.sleep(0.02)
        assert SESSION in app.state.active_turns

        tool_call = asyncio.create_task(
            client.post(
                f"/v1/sessions/{SESSION}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {"name": "files_find", "arguments": {"sha256": "0" * 64}},
                    "_omnigent_operation": {"id": "op-stop", "step": "s1"},
                },
            )
        )
        await asyncio.wait_for(tool_started.wait(), timeout=5)

        turn = app.state.active_turns[SESSION]
        started = time.monotonic()
        stop = asyncio.create_task(
            client.post(f"/v1/sessions/{SESSION}/events", json={"type": "interrupt"})
        )
        response = await asyncio.wait_for(tool_call, timeout=1.5)
        tool_elapsed = time.monotonic() - started
        await asyncio.wait({turn}, timeout=1.5)
        assert (await stop).status_code == 204

    assert tool_cancelled.is_set()
    assert response.json() == STOPPED_RESULT
    assert tool_elapsed < 1.0
    assert turn.done()
    assert SESSION not in app.state.active_turns


@pytest.mark.asyncio
async def test_a_tunnel_waiter_cancel_still_does_not_cancel_the_execution() -> None:
    registry = McpExecutionRegistry()
    release = asyncio.Event()
    started = asyncio.Event()

    async def work() -> McpExecutionResult:
        started.set()
        await release.wait()
        return McpExecutionResult(status_code=200, content={"result": "done"})

    waiter = asyncio.create_task(
        registry.execute(session_id="s", operation_id="o", step="1", params={}, run=work)
    )
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert registry.cancel_turn("other") == 0
    release.set()
    again = await registry.execute(session_id="s", operation_id="o", step="1", params={}, run=work)
    assert again.content == {"result": "done"}


@pytest.mark.asyncio
async def test_cancel_turn_answers_waiters_with_the_stopped_result() -> None:
    registry = McpExecutionRegistry()
    started = asyncio.Event()

    async def work() -> McpExecutionResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    waiter = asyncio.create_task(
        registry.execute(session_id="s", operation_id="o", step="1", params={}, run=work)
    )
    await started.wait()
    assert registry.cancel_turn("s") == 1
    assert (await asyncio.wait_for(waiter, timeout=1)).content == STOPPED_RESULT
