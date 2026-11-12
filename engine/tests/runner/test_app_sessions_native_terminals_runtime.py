"""Tests for Codex and Claude terminal runtime behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    ResolvedSpec,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_create_session_threads_workspace_to_pi_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi pre-spawn receives the session workspace, not the bundle dir."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    # Isolate $HOME too: ambient provider detection reads ~/.databrickscfg,
    # which on a multi-profile Databricks dev box otherwise 400s the create
    # with "match <host>; use --profile" (issue #4279).
    monkeypatch.setenv("HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "config-home"))
    session_id = "18f39ab73f49285e4dab0c80ff7b8455"
    runner_workspace = tmp_path / "runner-workspace"
    runner_workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / "86c64e0f9cab937bd04c79b9957dd55a-v1"
    bundle_dir.mkdir(parents=True)
    worktree = tmp_path / "repo-worktrees" / "feature-x"
    worktree.mkdir(parents=True)
    spec = AgentSpec(
        spec_version=1,
        name="pi-worktree-agent",
        skills_filter="none",
        executor=ExecutorSpec(config={"harness": "pi"}),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    class _SessionWorkspaceClient(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del kwargs
            if url == f"/v1/sessions/{session_id}":
                return httpx.Response(
                    200,
                    json={
                        "id": session_id,
                        "agent_id": "86c64e0f9cab937bd04c79b9957dd55a",
                        "created_at": 1.0,
                        "workspace": str(worktree),
                    },
                    request=httpx.Request("GET", url),
                )
            return await super().get(url)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionWorkspaceClient(),  # type: ignore[arg-type]
        runner_workspace=runner_workspace,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "86c64e0f9cab937bd04c79b9957dd55a"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == session_id
    assert harness == "pi"
    assert env is not None
    assert env["HARNESS_PI_CWD"] == str(worktree.resolve())
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(bundle_dir)


@pytest.mark.parametrize("workspace_value", [None, "   "])
@pytest.mark.asyncio
async def test_create_session_threads_runner_workspace_to_pi_cwd_when_session_workspace_missing(
    workspace_value: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi pre-spawn falls back to runner workspace when session workspace is empty."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    # Isolate $HOME too: ambient provider detection reads ~/.databrickscfg,
    # which on a multi-profile Databricks dev box otherwise 400s the create
    # with "match <host>; use --profile" (issue #4279).
    monkeypatch.setenv("HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "config-home"))
    session_id = "3f1d20a97a7d0ba93e02cf17aeb92367"
    runner_workspace = tmp_path / "runner-workspace"
    runner_workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / "86c64e0f9cab937bd04c79b9957dd55a-v1"
    bundle_dir.mkdir(parents=True)
    spec = AgentSpec(
        spec_version=1,
        name="pi-runner-workspace-agent",
        skills_filter="none",
        executor=ExecutorSpec(config={"harness": "pi"}),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    class _SessionWorkspaceClient(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del kwargs
            if url == f"/v1/sessions/{session_id}":
                return httpx.Response(
                    200,
                    json={
                        "id": session_id,
                        "agent_id": "86c64e0f9cab937bd04c79b9957dd55a",
                        "created_at": 1.0,
                        "workspace": workspace_value,
                    },
                    request=httpx.Request("GET", url),
                )
            return await super().get(url)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionWorkspaceClient(),  # type: ignore[arg-type]
        runner_workspace=runner_workspace,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "86c64e0f9cab937bd04c79b9957dd55a"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == session_id
    assert harness == "pi"
    assert env is not None
    assert env["HARNESS_PI_CWD"] == str(runner_workspace.resolve())
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(bundle_dir)
