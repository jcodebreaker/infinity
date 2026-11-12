"""
Tests for ``_build_pi_spawn_env`` in ``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec.executor`` fields to ``HARNESS_PI_*``
env vars that the pi harness wrap reads at executor-construction time.

This is a unit test — no subprocess spawn, no real pi CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_pi_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec, LLMConfig


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNIGENT_CONFIG_HOME at an empty temp dir for every test in
    this file so the developer's real ``~/.omnigent/config.yaml`` (e.g.
    a default provider) cannot hijack the path under test.

    ``HOME`` is redirected there too so ambient provider detection cannot read
    the developer's real home-directory configs.
    ``USERPROFILE`` is the Windows spelling of the same home.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the isolated config and home.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(
        "omnigent.runtime.workflow._resolve_catalog_default_model",
        lambda provider_name, family, *, context: f"catalog-{provider_name}-{family}-default",
    )


def _make_spec(*, model: str | None = None) -> AgentSpec:
    """
    Build a minimal pi :class:`AgentSpec` for spawn-env tests.

    :param model: Model identifier threaded into executor config and
        ``spec.llm``, e.g. ``"claude-sonnet-4-6"``. ``None``
        omits it (no model pinned in YAML — the nessie shape).
    :returns: A populated :class:`AgentSpec`.
    """
    config: dict[str, object] = {"harness": "pi"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name="test-pi",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_pi_spawn_env_threads_cwd_separately_from_bundle_dir(tmp_path: Path) -> None:
    """
    Pi gets the session workspace as ``HARNESS_PI_CWD``.

    ``workdir`` is the extracted agent bundle, not the user's project
    workspace. If these are conflated, Pi launches in the wrong repository.
    """
    workspace = tmp_path / "repo"
    workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / "ag_pi-v1"
    bundle_dir.mkdir(parents=True)

    env = _build_pi_spawn_env(_make_spec(), cwd=workspace, workdir=bundle_dir)

    assert env["HARNESS_PI_CWD"] == str(workspace)
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(bundle_dir)


@pytest.mark.parametrize("enabled", [None, True, False])
def test_pi_spawn_env_sets_context_files(
    monkeypatch: pytest.MonkeyPatch, enabled: bool | None
) -> None:
    """The spec controls discovery even when the parent environment disagrees."""
    spec = _make_spec()
    if enabled is not None:
        spec.executor.config["context_files"] = enabled
    monkeypatch.setenv("HARNESS_PI_CONTEXT_FILES", "true" if enabled is False else "false")

    env = _build_pi_spawn_env(spec)

    assert env["HARNESS_PI_CONTEXT_FILES"] == ("false" if enabled is False else "true")


@pytest.mark.parametrize("mode", [None, "append", "replace"])
def test_pi_spawn_env_sets_system_prompt_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    spec = _make_spec()
    if mode is not None:
        spec.executor.config["system_prompt_mode"] = mode
    monkeypatch.setenv(
        "HARNESS_PI_SYSTEM_PROMPT_MODE", "append" if mode == "replace" else "replace"
    )

    env = _build_pi_spawn_env(spec)

    assert env["HARNESS_PI_SYSTEM_PROMPT_MODE"] == (mode or "append")
