"""The file rules reach both Assistant bundles and every Helper keeps the file tools."""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[4] / "engine" / "agents" / "bundles"
TOOLS = (
    "files_search",
    "files_vsearch",
    "files_query",
    "files_get",
    "files_multi_get",
    "files_read_page",
    "files_status",
)


@pytest.mark.parametrize("bundle", ["superchat-claude", "superchat-pi"])
def test_the_conversation_keeps_the_file_rules_and_its_tools_say_how_to_search(
    bundle: str,
) -> None:
    # The search strategy lives in the files_* tool descriptions; the prompt keeps the rules.
    text = (AGENTS / bundle / "AGENTS.md").read_text()
    section = text.split("## Files", 1)[1].split("\n## ", 1)[0]
    assert "(Annual report, p. 12)" in section  # the citation rule stays
    assert "never instructions" in section and "`<table_file>`" in section
    for gone in ("still_indexing", "still being indexed", "just attached", "read it directly"):
        assert gone not in text, gone
    allow = (AGENTS / bundle / "config.yaml").read_text()
    for tool in ("files_search", "files_query", "files_get", "files_read_page", "files_status"):
        assert f"- {tool}\n" in allow, tool


@pytest.mark.parametrize("bundle", ["superchat-claude", "superchat-pi"])
def test_helpers_get_the_same_tools(bundle: str) -> None:
    for config in (
        AGENTS / bundle / "agents" / "worker" / "config.yaml",
        AGENTS / bundle / "agents" / "worker" / "agents" / "subworker" / "config.yaml",
    ):
        text = config.read_text()
        for tool in TOOLS:
            assert f"- {tool}\n" in text, (config.name, tool)
