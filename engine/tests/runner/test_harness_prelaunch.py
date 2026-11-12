"""The Claude CLI launches at session init, before the person speaks (ADR 0026)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.test_suppress_recovery_turn import (
    AGENT_ID,
    SESSION_ID,
    _CatchUpServerClient,
    _session_init_payload,
)


class _GatedHarnessClient(_ScriptedHarnessClient):
    """Holds the first POST (the pre-launch) open until the test releases it."""

    def __init__(self, frames: list[str]) -> None:
        super().__init__(frames)
        self.release_first = asyncio.Event()
        self.order: list[str] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        ctx = super().stream(method, url, json=json, timeout=timeout)
        first = len(self.posted_bodies) == 1
        gate = self.release_first
        order = self.order

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> Any:
                order.append("prelaunch" if first else "turn")
                if first:
                    await gate.wait()
                    order.append("prelaunch done")
                return await ctx.__aenter__()

            async def __aexit__(self, *exc: object) -> None:
                await ctx.__aexit__(*exc)

        return _Ctx()


def _app(harness: _GatedHarnessClient) -> Any:
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_CatchUpServerClient(),  # type: ignore[arg-type]
    )


_FRAMES = [
    _sse({"type": "response.created", "response": {"id": "resp_1"}}),
    _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
]


@pytest.mark.asyncio
async def test_a_hung_prelaunch_costs_the_first_turn_its_bound_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.runner.app._PRELAUNCH_WAIT_SECONDS", 0.2)
    harness = _GatedHarnessClient(_FRAMES)  # the pre-launch is never released
    async with _runner_client(_app(harness)) as client:
        init = await client.post(
            "/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True)
        )
        assert init.status_code == 201, init.text
        forwarded = await client.post(
            f"/v1/sessions/{SESSION_ID}/events",
            json={
                "type": "message",
                "agent_id": AGENT_ID,
                "content": [{"type": "input_text", "text": "make the deck"}],
            },
        )
        assert forwarded.status_code == 202, forwarded.text
        for _ in range(300):
            if len(harness.posted_bodies) == 2:
                break
            await asyncio.sleep(0.01)

    assert len(harness.posted_bodies) == 2, "the turn must post once the bound passes"
    assert "make the deck" in str(harness.posted_bodies[1])


@pytest.mark.asyncio
async def test_session_init_launches_the_cli_and_the_first_turn_waits_for_it() -> None:
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    harness = _GatedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_CatchUpServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        init = await client.post(
            "/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True)
        )
        assert init.status_code == 201, init.text
        for _ in range(200):
            if harness.posted_bodies:
                break
            await asyncio.sleep(0.01)
        assert len(harness.posted_bodies) == 1, "session init did not pre-launch the CLI"
        prelaunch = harness.posted_bodies[0]
        assert prelaunch["type"] == "message"
        assert prelaunch["content"] == []
        assert prelaunch["instructions"]

        forwarded = await client.post(
            f"/v1/sessions/{SESSION_ID}/events",
            json={
                "type": "message",
                "agent_id": AGENT_ID,
                "content": [{"type": "input_text", "text": "make the deck"}],
            },
        )
        assert forwarded.status_code == 202, forwarded.text
        await asyncio.sleep(0.2)
        assert len(harness.posted_bodies) == 1, "the turn must wait for the pre-launch"

        harness.release_first.set()
        for _ in range(300):
            if len(harness.posted_bodies) == 2:
                break
            await asyncio.sleep(0.01)

    assert len(harness.posted_bodies) == 2
    assert harness.order[:3] == ["prelaunch", "prelaunch done", "turn"]
    assert "make the deck" in str(harness.posted_bodies[1])


def _launch_fields(body: dict[str, Any]) -> tuple[Any, ...]:
    """What the Claude CLI is launched with: its instructions, tool names, mode and effort."""
    tools = sorted(t.get("name") or t.get("function", {}).get("name") for t in body["tools"])
    return body["instructions"], tools, body.get("context_mode"), body.get("reasoning")


@pytest.mark.asyncio
async def test_the_prelaunch_matches_a_turn_sent_from_the_open_screen() -> None:
    """A turn sent while the person's screen is subscribed finds the CLI launched for it."""
    harness = _GatedHarnessClient(_FRAMES)
    harness.release_first.set()
    async with _runner_client(_app(harness)) as client:
        init = await client.post(
            "/v1/sessions", json=_session_init_payload(suppress_recovery_turn=True)
        )
        assert init.status_code == 201, init.text
        for _ in range(200):
            if harness.posted_bodies:
                break
            await asyncio.sleep(0.01)
        forwarded = await client.post(
            f"/v1/sessions/{SESSION_ID}/events",
            json={
                "type": "message",
                "agent_id": AGENT_ID,
                "browser_renderer_available": True,
                "content": [{"type": "input_text", "text": "make the deck"}],
            },
        )
        assert forwarded.status_code == 202, forwarded.text
        for _ in range(300):
            if len(harness.posted_bodies) == 2:
                break
            await asyncio.sleep(0.01)

    prelaunch, turn = harness.posted_bodies
    assert any("browser" in name for name in _launch_fields(turn)[1])
    assert _launch_fields(prelaunch) == _launch_fields(turn)
