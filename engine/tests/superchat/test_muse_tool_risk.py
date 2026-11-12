"""Every tool the Muse may call has an explicit risk class.

A call the approvals policy classifies is ASKed. A new tool whose name happens to carry a
risky verb ("publish", "delete", "share") would silently start asking, and a risky tool
named innocently would silently stop. Pinning each tool here forces that decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.superchat.approvals.policy import classify_tool_call
from omnigent.superchat.feature import ToolManagerCtx
from omnigent.superchat.features import _load

REPO = Path(__file__).resolve().parents[3]
BUNDLES = [
    REPO / "engine/agents/bundles" / name / "config.yaml"
    for name in ("superchat-claude", "superchat-pi")
]
MUSE_LABELS = {"omnigent.context.mode": "superside-chat", "omnigent.superchat.muse": "true"}

# ``None``: never asks. Shell, browser and other ``sys_*`` / ``memory_*`` / ``browser_*`` /
# ``objective_*`` tools are classified by their arguments or prefix, not listed here.
RISK: dict[str, str | None] = {
    "artifact_delete": "delete",
    "artifact_list": None,
    "artifact_outline": None,
    "artifact_publish": "post",
    "artifact_save": None,
    "ask_clarification": None,
    "checklist": None,
    "context_brief": None,
    "deck_check": None,
    "deck_export": None,
    "deck_new": None,
    "deck_theme_set": None,
    "deck_themes": None,
    "files_get": None,
    "files_multi_get": None,
    "files_query": None,
    "files_read_page": None,
    "files_search": None,
    "files_status": None,
    "files_vsearch": None,
    "live": None,
    "load_skill": None,
    "open_project": None,
    "reach_rules": None,
    "recall": None,
    "recall_get": None,
    "render_card": None,
    "schedule": None,
    "session_history": None,
    "state_close": None,
    "start_helper": None,
    "message_helper": None,
    "suggest_follow_ups": None,
    "suggestion_create": None,
    "suggestion_list": None,
    "topics_update": None,
    "vault_fill": None,
    "vault_request_secret": None,
    "web_search": None,
}

_PREFIX_CLASSIFIED = ("sys_", "memory_", "browser_", "objective_", "goal_")


def _muse_tools() -> set[str]:
    """Feature tools the Muse is given, plus the bundles' explicitly allowed tools."""
    names = {
        tool.name() for feature in _load() for tool in feature.tools(MUSE_LABELS, ToolManagerCtx())
    }
    for bundle in BUNDLES:
        allow = yaml.safe_load(bundle.read_text())["tools"]["allow"]
        names |= {name for name in allow if "*" not in name}
    return {name for name in names if not name.startswith(_PREFIX_CLASSIFIED)}


def test_every_muse_tool_has_an_explicit_risk_class() -> None:
    missing = sorted(_muse_tools() - RISK.keys())
    assert not missing, f"give these tools a risk class in RISK: {missing}"


@pytest.mark.parametrize("name", sorted(RISK))
def test_risk_class_matches_the_approvals_policy(name: str) -> None:
    risk = classify_tool_call(name, {})
    assert (risk.category if risk else None) == RISK[name]
    # Namespaced through the engine's MCP server, the class is the same.
    risk = classify_tool_call(f"mcp__omnigent__{name}", {})
    assert (risk.category if risk else None) == RISK[name]
