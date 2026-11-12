"""``/mcp/execute`` lets a relay-only op through only for the engine's own tunnelled relay call."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner import tool_dispatch as _tool_dispatch
from omnigent.spec.types import AgentSpec
from omnigent.superchat.feature import RELAY_MARK
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

SESSION = "38f6cf055029a2a23b227a8305f76c9d"


async def _call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict, *, tunnel: bool):
    seen: list[bool] = []

    async def fake_execute_tool(*, relay: bool = False, **kwargs: Any) -> str:
        del kwargs
        seen.append(relay)
        return "{}"

    monkeypatch.setattr(_tool_dispatch, "execute_tool", fake_execute_tool)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1)

    harness = _ScriptedHarnessClient(
        [_sse({"type": "response.completed", "response": {"id": "resp_1"}})]
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    transport = httpx.ASGITransport(app=app, client=("tunnel", 0) if tunnel else ("10.0.0.9", 5))
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        seed = await client.post(
            f"/v1/sessions/{SESSION}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "probe-agent",
                "content": [{"type": "input_text", "text": "seed"}],
                "harness": "claude-sdk",
            },
        )
        assert seed.status_code == 202
        for _ in range(100):
            if harness.posted_bodies:
                break
            await asyncio.sleep(0.05)
        resp = await client.post(f"/v1/sessions/{SESSION}/mcp/execute", json=body)
    assert resp.status_code == 200, resp.text
    return seen


def _body(**extra: Any) -> dict:
    return {
        "method": "tools/call",
        "params": {"name": "files_find", "arguments": {"sha256": "0" * 64}},
        **extra,
    }


@pytest.mark.asyncio
async def test_the_tunnelled_relay_call_with_the_mark_is_a_relay(tmp_path, monkeypatch) -> None:
    assert await _call(tmp_path, monkeypatch, _body(**{RELAY_MARK: True}), tunnel=True) == [True]


@pytest.mark.asyncio
async def test_a_call_without_the_mark_is_not_a_relay(tmp_path, monkeypatch) -> None:
    assert await _call(tmp_path, monkeypatch, _body(), tunnel=True) == [False]


@pytest.mark.asyncio
async def test_the_mark_on_a_direct_connection_is_stripped(tmp_path, monkeypatch) -> None:
    assert await _call(tmp_path, monkeypatch, _body(**{RELAY_MARK: True}), tunnel=False) == [False]


@pytest.mark.asyncio
async def test_the_mark_survives_the_retained_operation_path(tmp_path, monkeypatch) -> None:
    body = _body(**{RELAY_MARK: True, "_omnigent_operation": {"id": "op-1", "step": "s1"}})
    assert await _call(tmp_path, monkeypatch, body, tunnel=True) == [True]
