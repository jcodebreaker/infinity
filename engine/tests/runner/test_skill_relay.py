"""Skill tools reach native harnesses through the relay.

A native session ignores ``request.tools`` and sees only
``build_native_relay_tool_schemas``. ``load_skill`` is what discovers
host-scope skills — ``.agents/skills``, ``.claude/skills`` and their
home-directory equivalents — so if it is absent from the relay, a skill dropped
where the docs say every agent picks it up is invisible to every native
harness, silently.

The per-family sources do not cover it: codex walks its bundle plus
``~/.codex/skills``.
"""

from __future__ import annotations

from pathlib import Path


def test_relayed_load_skill_discovers_a_host_scope_skill(tmp_path: Path) -> None:
    """The relayed tool reaches the directories the docs advertise.

    Membership in the union is only half the claim; the other half is that
    ``load_skill`` run from a native session's workspace actually finds a skill
    dropped in ``.claude/skills``, which is what makes relaying it worth doing.
    """
    from omnigent.runner.tool_dispatch import _execute_skill_tool

    skill_dir = tmp_path / ".claude" / "skills" / "host-only"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: host-only\ndescription: a host-scope skill\n---\n\nthe skill body\n"
    )

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "host-only"},
        agent_spec=None,
        runner_workspace=tmp_path,
    )

    assert "the skill body" in loaded, loaded[:300]
