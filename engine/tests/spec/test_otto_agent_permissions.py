"""Contract tests for unattended Otto agent permission modes."""

from pathlib import Path

import pytest
import yaml

# The repro and resolve dev agents these tests read were removed with the engine's dev tools
# (ADR 0020, commit 5d014befe); the maintainer decides whether the file goes with them.
pytestmark = pytest.mark.skip(
    reason="dev/repro-agent and dev/resolve-agent were removed (ADR 0020)"
)

_ROOT = Path(__file__).resolve().parents[2]


def _executor_config(relative_path: str) -> dict[str, object]:
    config = yaml.safe_load((_ROOT / relative_path).read_text())
    return config["executor"]["config"]


def test_repro_agent_fully_bypasses_sdk_permission_prompts() -> None:
    assert _executor_config("dev/repro-agent/config.yaml")["permission_mode"] == (
        "bypassPermissions"
    )


def test_resolve_agent_fully_bypasses_sdk_permission_prompts() -> None:
    assert _executor_config("dev/resolve-agent/config.yaml")["permission_mode"] == (
        "bypassPermissions"
    )
