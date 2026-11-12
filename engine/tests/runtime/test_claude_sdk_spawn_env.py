"""
Tests for ``_build_claude_sdk_spawn_env`` in
``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec.executor`` fields to
``HARNESS_CLAUDE_SDK_*`` env vars that the claude-sdk harness wrap reads
at executor-construction time.

This is a unit test — no subprocess spawn, no real claude CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from omnigent.runtime.workflow import _build_claude_sdk_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNIGENT_CONFIG_HOME at an empty temp dir for every test in
    this file so tests that don't explicitly set up a global config are
    not affected by the developer's real ``~/.omnigent/config.yaml``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the isolated config.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        "omnigent.runtime.workflow._resolve_catalog_default_model",
        lambda provider_name, family, *, context: f"catalog-{provider_name}-{family}-default",
    )


def _make_spec(
    *,
    model: str | None = "claude-sonnet-4-6",
    auth: ApiKeyAuth | None = None,
) -> AgentSpec:
    """
    Build a minimal claude-sdk :class:`AgentSpec` for spawn-env tests.

    :param model: Model identifier threaded into executor config and
        ``spec.llm``, e.g. ``"claude-sonnet-4-6"``.
    :param auth: Typed auth object placed on ``spec.executor.auth``.
        ``None`` omits it (harness falls back to global config).
    :returns: A populated :class:`AgentSpec`.
    """
    config: dict[str, object] = {"harness": "claude-sdk"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name="test-claude-sdk",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_anthropic_prefixed_model_is_stripped_for_the_claude_cli() -> None:
    """
    ``executor.model: anthropic/<name>`` hands the claude CLI the bare
    ``<name>`` — the CLI rejects vendor-prefixed model ids.

    Failure means a spec pinning the provider-routed spelling gets its
    prefixed id forwarded verbatim to the Anthropic Messages endpoint,
    where the claude CLI errors with "There's an issue with the selected
    model".
    """
    spec = _make_spec(model="anthropic/claude-sonnet-4-6")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "claude-sonnet-4-6"


def test_bare_model_is_passed_through_unchanged() -> None:
    """A bare (unprefixed) model id reaches the claude CLI as written."""
    spec = _make_spec(model="claude-sonnet-4-6")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "claude-sonnet-4-6"


def test_api_key_auth_sets_helper_env_var() -> None:
    """
    ``executor.auth: {type: api_key, api_key: …}`` sets
    ``HARNESS_CLAUDE_SDK_API_KEY_HELPER`` to a printf shell command.

    Failure means the API key never reaches the Claude CLI's
    ``settings.apiKeyHelper`` and the agent falls back to subscription
    auth silently.
    """
    spec = _make_spec(model=None, auth=ApiKeyAuth(api_key="sk-ant-test-123"))
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert "HARNESS_CLAUDE_SDK_API_KEY_HELPER" in env
    # The helper command must echo the literal key (shlex-quoted for safety).
    assert "sk-ant-test-123" in env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"]
    # api_key auth without a base_url does not trigger gateway routing.
    assert "HARNESS_CLAUDE_SDK_GATEWAY" not in env


def test_api_key_auth_with_special_chars_is_shell_safe() -> None:
    """
    API keys containing shell-special characters (spaces, quotes, ``$``)
    are safely quoted in the helper command via ``shlex.quote``.

    Failure means a key like ``sk-$weird`` could be misinterpreted by
    the shell when the Claude CLI invokes the helper command.
    """
    spec = _make_spec(model=None, auth=ApiKeyAuth(api_key="sk-$weird 'key'"))
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    helper = env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"]
    # The raw key must NOT appear unquoted.
    assert "sk-$weird 'key'" not in helper
    # shlex-quoted form must be present.
    assert "sk-" in helper


def test_global_config_api_key_applied_when_spec_has_no_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    When the spec declares no auth, ``_load_global_auth()`` is consulted
    and a global ``auth: {type: api_key, api_key: …}`` is applied.

    Failure means global auth configuration is silently ignored for
    claude-sdk agents.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_yaml.dump({"auth": {"type": "api_key", "api_key": "sk-global"}}))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    spec = _make_spec(auth=None)
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert "sk-global" in env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"]
    assert "HARNESS_CLAUDE_SDK_GATEWAY" not in env
