"""``_find_spec_by_name`` resolves sub-agents by searching the spec tree."""

from __future__ import annotations

from omnigent.runtime.workflow import _find_spec_by_name
from omnigent.spec.types import AgentSpec


def _parent() -> AgentSpec:
    panelist = AgentSpec(spec_version=1, name="panelist")
    return AgentSpec(spec_version=1, name="concordia", sub_agents=[panelist])


def test_declared_sub_agent_resolves_from_tree() -> None:
    found = _find_spec_by_name(_parent(), "panelist")
    assert found is not None
    assert found.name == "panelist"


def test_missing_name_returns_none() -> None:
    """An unknown name resolves to ``None`` so a misconfiguration fails loud."""
    assert _find_spec_by_name(_parent(), "does-not-exist") is None
