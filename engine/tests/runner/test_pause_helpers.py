"""Pause holds a Helper at its next tool boundary; resume lets it go on; 30 minutes stop it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.app import session_status_event
from omnigent.runner.mcp_execution_registry import (
    PAUSE_LIMIT_S,
    PAUSED_NOTE,
    STOPPED_RESULT,
    McpExecutionRegistry,
    McpExecutionResult,
)
from omnigent.server.schemas import SessionStatusEvent
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient

HELPER = "5b0f6f3c1d2a4e5f8a9b0c1d2e3f4a5b"
PARENT = "0a0f6f3c1d2a4e5f8a9b0c1d2e3f4a5b"


class _Clock:
    """A fake sleep: each sleeper waits until :meth:`advance` passes its deadline."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Event]] = []

    async def sleep(self, seconds: float) -> None:
        done = asyncio.Event()
        self._sleepers.append((self.now + seconds, done))
        await done.wait()

    async def advance(self, seconds: float) -> None:
        await asyncio.sleep(0)
        self.now += seconds
        for deadline, done in self._sleepers:
            if deadline <= self.now:
                done.set()
        for _ in range(5):
            await asyncio.sleep(0)


def _ran(log: list[str], name: str):
    async def run() -> McpExecutionResult:
        log.append(name)
        return McpExecutionResult(status_code=200, content={"result": name})

    return run


async def _call(registry: McpExecutionRegistry, session: str, name: str, log: list[str]) -> Any:
    """A tool boundary as the runner has it: hold, then run (stopped when the pause expired)."""
    if not await registry.hold(session):
        return McpExecutionResult(status_code=200, content=STOPPED_RESULT)
    return await _ran(log, name)()


@pytest.mark.asyncio
async def test_a_paused_session_holds_at_its_next_call_and_resume_continues() -> None:
    clock = _Clock()
    registry = McpExecutionRegistry(sleep=clock.sleep)
    held: list[str] = []
    log: list[str] = []
    assert registry.pause("helper", on_hold=held.append)
    assert not registry.pause("helper")

    call = asyncio.create_task(_call(registry, "helper", "next", log))
    other = await _call(registry, "conversation", "free", log)
    await asyncio.sleep(0)
    assert other.content == {"result": "free"}
    assert log == ["free"] and held == ["helper"] and not call.done()

    assert registry.resume("helper") is True
    assert (await asyncio.wait_for(call, 1)).content == {"result": "next"}
    assert not registry.is_paused("helper")
    assert registry.resume("helper") is False


@pytest.mark.asyncio
async def test_a_pause_left_for_thirty_minutes_stops_the_held_call() -> None:
    clock = _Clock()
    registry = McpExecutionRegistry(sleep=clock.sleep)
    expired: list[str] = []

    async def on_expired(session_id: str) -> None:
        expired.append(session_id)

    log: list[str] = []
    registry.pause("helper", on_expired=on_expired)
    call = asyncio.create_task(_call(registry, "helper", "next", log))
    await clock.advance(PAUSE_LIMIT_S - 1)
    assert not call.done() and expired == []
    await clock.advance(1)
    assert (await asyncio.wait_for(call, 1)).content == STOPPED_RESULT
    assert expired == ["helper"] and log == [] and not registry.is_paused("helper")


@pytest.mark.asyncio
async def test_resume_and_stop_cancel_the_expiry() -> None:
    clock = _Clock()
    registry = McpExecutionRegistry(sleep=clock.sleep)
    expired: list[str] = []

    async def on_expired(session_id: str) -> None:
        expired.append(session_id)

    registry.pause("a", on_expired=on_expired)
    registry.pause("b", on_expired=on_expired)
    registry.resume("a")
    registry.cancel_turn("b")
    await clock.advance(PAUSE_LIMIT_S * 2)
    assert expired == []


class _Body(NullServerClient._Response):
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _Server(NullServerClient):
    """Records the runner's calls to the engine; the Helper reads as titled with a parent and
    carries *labels*."""

    def __init__(self, labels: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.labels = labels or {}

    async def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("GET", url, None))
        if url == f"/v1/sessions/{HELPER}":
            return _Body({"id": HELPER, "title": "Q3 deck", "parent_session_id": PARENT})
        if url == f"/v1/sessions/{HELPER}/labels":
            return _Body({"labels": self.labels})
        return self._Response()

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("POST", url, kwargs.get("json")))
        return self._Response()

    async def patch(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("PATCH", url, kwargs.get("json")))
        return self._Response()


@pytest.mark.asyncio
async def test_runner_pause_resume_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    released = asyncio.Event()

    async def instant(_seconds: float) -> None:
        await released.wait()

    # The app builds its own registry; its expiry waits on this gate instead of 30 minutes.
    real_init = McpExecutionRegistry.__init__
    monkeypatch.setattr(
        McpExecutionRegistry,
        "__init__",
        lambda self, **_: real_init(self, sleep=instant),
    )
    server = _Server()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    registry: McpExecutionRegistry = app.state.mcp_execution_registry
    transport = httpx.ASGITransport(app=app, client=("tunnel", 0))
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        events = f"/v1/sessions/{HELPER}/events"
        assert (await client.post(events, json={"type": "pause"})).status_code == 204
        assert registry.is_paused(HELPER)
        assert (await client.post(events, json={"type": "resume"})).status_code == 204
        assert not registry.is_paused(HELPER)

        await client.post(events, json={"type": "pause"})
        released.set()
        for _ in range(50):
            if any(c[1] == f"/v1/sessions/{PARENT}/events" for c in server.calls):
                break
            await asyncio.sleep(0.01)

    assert ("POST", events, {"type": "interrupt"}) in server.calls
    assert (
        "PATCH",
        f"/v1/sessions/{HELPER}",
        {"labels": {"superchat.paused": ""}},
    ) in server.calls
    notices = [c[2] for c in server.calls if c[1] == f"/v1/sessions/{PARENT}/events"]
    assert len(notices) == 1
    text = notices[0]["data"]["content"][0]["text"]
    assert text == "Q3 deck was paused for 30 minutes and stopped."
    assert notices[0]["data"]["is_system_notice"] is True


def _runner(server: _Server, tmp_path: Path, **registry: Any) -> Any:
    real_init = McpExecutionRegistry.__init__
    patch = pytest.MonkeyPatch()
    patch.setattr(McpExecutionRegistry, "__init__", lambda self, **_: real_init(self, **registry))
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    try:
        return create_runner_app(
            process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
            server_client=server,  # type: ignore[arg-type]
            runner_workspace=workspace,
        )
    finally:
        patch.undo()


@pytest.mark.asyncio
async def test_a_built_in_tool_and_a_native_shell_call_are_both_held_and_both_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import app as runner_app
    from omnigent.runner import tool_dispatch

    ran: list[str] = []

    async def fake_execute_tool(**kwargs: Any) -> str:
        ran.append(kwargs["tool_name"])
        return "{}"

    monkeypatch.setattr(tool_dispatch, "execute_tool", fake_execute_tool)
    server = _Server()
    app = _runner(server, tmp_path)
    registry: McpExecutionRegistry = app.state.mcp_execution_registry
    transport = httpx.ASGITransport(app=app, client=("tunnel", 0))
    verdicts: list[dict[str, Any]] = []

    class _Harness:
        async def post(self, url: str, **kwargs: Any) -> Any:
            verdicts.append(kwargs["json"])
            return httpx.Response(204)

    class _Policies(_Server):
        async def post(self, url: str, **kwargs: Any) -> Any:
            self.calls.append(("POST", url, kwargs.get("json")))
            return _Body({"result": "POLICY_ACTION_ALLOW"})

    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post(f"/v1/sessions/{HELPER}/events", json={"type": "pause"})
        save = asyncio.create_task(
            client.post(
                f"/v1/sessions/{HELPER}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {"name": "artifact_save", "arguments": {"path": "q3.deck.html"}},
                },
            )
        )
        shell = asyncio.create_task(
            runner_app._evaluate_policy_via_omnigent(
                server_client=_Policies(),  # type: ignore[arg-type]
                harness_client=_Harness(),  # type: ignore[arg-type]
                conversation_id=HELPER,
                evaluation_id="poleval_1",
                phase="PHASE_TOOL_CALL",
                data={"name": "Bash", "arguments": {"command": "ls"}},
                hold=registry.hold,
            )
        )
        await asyncio.sleep(0.1)
        assert ran == [] and verdicts == [] and not save.done() and not shell.done()

        assert (
            await client.post(f"/v1/sessions/{HELPER}/events", json={"type": "resume"})
        ).status_code == 204
        assert (await asyncio.wait_for(save, 2)).status_code == 200
        await asyncio.wait_for(shell, 2)

    assert ran == ["artifact_save"]
    assert verdicts[0]["action"] == "POLICY_ACTION_ALLOW"


@pytest.mark.asyncio
async def test_an_expired_pause_denies_the_held_native_call() -> None:
    from omnigent.runner import app as runner_app

    clock = _Clock()
    registry = McpExecutionRegistry(sleep=clock.sleep)
    registry.pause(HELPER)
    verdicts: list[dict[str, Any]] = []

    class _Harness:
        async def post(self, url: str, **kwargs: Any) -> Any:
            verdicts.append(kwargs["json"])
            return httpx.Response(204)

    server = _Server()
    call = asyncio.create_task(
        runner_app._evaluate_policy_via_omnigent(
            server_client=server,  # type: ignore[arg-type]
            harness_client=_Harness(),  # type: ignore[arg-type]
            conversation_id=HELPER,
            evaluation_id="poleval_2",
            phase="PHASE_TOOL_CALL",
            data={"name": "Bash"},
            hold=registry.hold,
        )
    )
    await clock.advance(PAUSE_LIMIT_S)
    await asyncio.wait_for(call, 1)
    assert verdicts[0]["action"] == "POLICY_ACTION_DENY"
    assert not any("/policies/evaluate" in c[1] for c in server.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(("age", "expired"), [(60.0, False), (PAUSE_LIMIT_S + 60.0, True)])
async def test_a_restarted_runner_restores_the_pause_from_the_label(
    tmp_path: Path, age: float, expired: bool
) -> None:
    now = 1_000_000.0
    clock = _Clock()
    server = _Server(labels={"superchat.paused": str(int(now - age))})
    app = _runner(server, tmp_path, sleep=clock.sleep, clock=lambda: now)
    registry: McpExecutionRegistry = app.state.mcp_execution_registry
    transport = httpx.ASGITransport(app=app, client=("tunnel", 0))
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        await client.post(
            f"/v1/sessions/{HELPER}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "probe-agent",
                "content": [{"type": "input_text", "text": "go on"}],
                "harness": "claude-sdk",
            },
        )
        for _ in range(50):
            await asyncio.sleep(0.01)
            if expired and any(c[1] == f"/v1/sessions/{PARENT}/events" for c in server.calls):
                break
        if not expired:
            assert registry.is_paused(HELPER)
            # The rest of the 30 minutes counts from the label, not from the restart.
            await clock.advance(PAUSE_LIMIT_S - age - 1)
            assert registry.is_paused(HELPER)
            await clock.advance(1)
            await asyncio.sleep(0.05)
        assert not registry.is_paused(HELPER)
    assert ("POST", f"/v1/sessions/{HELPER}/events", {"type": "interrupt"}) in server.calls


def test_a_held_helpers_status_says_paused_as_a_flag() -> None:
    held = session_status_event("running", PAUSED_NOTE, paused=True)
    assert held == {
        "type": "session.status",
        "status": "running",
        "blocked_on": PAUSED_NOTE,
        "paused": True,
    }
    assert "paused" not in session_status_event("running")
    # the server's relay keeps the flag on the stream it publishes
    relayed = SessionStatusEvent(conversation_id=HELPER, **held).model_dump()
    assert relayed["paused"] is True
    assert (
        SessionStatusEvent(conversation_id=HELPER, type="session.status", status="idle").paused
        is None
    )
