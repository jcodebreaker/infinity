"""A ``${VAR}`` model that survived spec loading fails the turn, never reaches a provider."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.runner.app import _build_spawn_env_from_spec
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(model: str) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="superchat-pi",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi"}, model=model),
    )


def test_expanded_model_reaches_pi_spawn_env() -> None:
    env = _build_spawn_env_from_spec(_spec("claude-sonnet-5-5"), "pi")
    assert env is not None
    assert env["HARNESS_PI_MODEL"] == "claude-sonnet-5-5"


@pytest.mark.parametrize("harness", ["pi", "claude-sdk"])
def test_unresolved_spec_model_fails_loudly(harness: str) -> None:
    with pytest.raises(OmnigentError, match=r"\$\{SUPERCHAT_CLAUDE_MODEL\}"):
        _build_spawn_env_from_spec(_spec("${SUPERCHAT_CLAUDE_MODEL}"), harness)


def test_unresolved_model_override_fails_loudly() -> None:
    with pytest.raises(OmnigentError, match=r"executor\.model"):
        _build_spawn_env_from_spec(_spec("claude-sonnet-5-5"), "pi", model_override="${X}")
