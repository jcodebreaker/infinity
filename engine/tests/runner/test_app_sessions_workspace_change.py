"""The runner picks up a session's new working directory after ``workspace_change``.

A Helper started in a folder is moved before its brief is posted, so the harness process its
first message launches starts there, on either executor; later moves reach its tool calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.runner import tool_dispatch as _tool_dispatch
from omnigent.spec.types import AgentSpec, ExecutorSpec, LocalToolInfo
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

SESSION = "c7f36aa769270cac30144784fad50acc"
AGENT = "31ebfedf721b44dabd76f662cb70a400"


class _ServerWithWorkspace(NullServerClient):
    """A server whose session row carries a workspace the test can change."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    class _Row(NullServerClient._Response):
        def __init__(self, workspace: Path) -> None:
            self._workspace = workspace

        def json(self) -> dict[str, Any]:
            return {"workspace": str(self._workspace), "agent_id": AGENT}

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url.startswith("/v1/sessions/"):
            return self._Row(self.workspace)
        return await super().get(url, **kwargs)


@pytest.mark.parametrize(
    ("harness", "cwd_var"), [("claude-sdk", "HARNESS_CLAUDE_SDK_CWD"), ("pi", "HARNESS_PI_CWD")]
)
async def test_the_first_turn_starts_in_the_moved_folder_and_later_moves_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, cwd_var: str
) -> None:
    """Moved before turn 1: the harness launches there. Moved again: turn 2's tools follow."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    bundle = tmp_path / "bundle"
    (bundle / "tools" / "python").mkdir(parents=True)
    (bundle / "tools" / "python" / "probe.py").write_text(
        "from omnigent_client.tools import tool\n\n@tool\ndef probe(text: str) -> str:\n"
        "    return text\n"
    )
    spec = AgentSpec(
        spec_version=1,
        name="probe-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
        local_tools=[LocalToolInfo(name="probe", path="tools/python/probe.py", language="python")],
    )
    seen: list[Path | None] = []

    async def fake_dispatch(*, runner_workspace: Path | None = None, **_: Any) -> str:
        seen.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "dispatch_tool_locally", fake_dispatch)
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": "probe",
                    "call_id": "call_1",
                    "arguments": json.dumps({"text": "x"}),
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    server = _ServerWithWorkspace(old)  # created in its parent's folder

    async def resolver(agent_id: str, session_id: str | None = None) -> Any:
        from omnigent.runner.app import ResolvedSpec

        return ResolvedSpec(spec=spec, workdir=bundle)

    manager = _FakeProcessManager(_ScriptedHarnessClient(frames))
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=server,  # type: ignore[arg-type]
        runner_workspace=tmp_path,
    )

    async def run_turn(expected_calls: int) -> None:
        resp = await client.post(
            f"/v1/sessions/{SESSION}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": AGENT,
                "model": "probe-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": harness,
            },
        )
        assert resp.status_code == 202
        for _ in range(100):
            if len(seen) >= expected_calls:
                return
            await asyncio.sleep(0.05)

    async def move(folder: Path) -> None:
        server.workspace = folder  # the server already stored it (PUT .../workspace)
        changed = await client.post(
            f"/v1/sessions/{SESSION}/events", json={"type": "workspace_change"}
        )
        assert changed.status_code == 204

    async with _runner_client(app) as client:
        await move(new)
        await run_turn(1)
        await move(old)
        await run_turn(2)

    first_spawn_env = manager.get_client_calls[0][2]
    assert first_spawn_env[cwd_var] == str(new.resolve())
    assert seen == [new.resolve(), old.resolve()]
