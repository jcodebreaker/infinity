"""
Shared helpers for exposing an agent bundle's skills to a Claude harness.

Both the Claude Agent SDK executor (in-process, ``claude_sdk_executor``)
and the ``claude-native`` CLI launch path expose a bundle's
``skills/<dir>/SKILL.md`` files to Claude Code through its plugin
convention (``--plugin-dir <bundle>``). This module centralizes the two
pieces that wiring needs so the SDK and native paths stay in lockstep:
writing the bundle's ``.claude-plugin/plugin.json`` manifest, and
translating the spec's ``skills_filter`` into the Claude Code CLI args
(``--plugin-dir`` + ``--setting-sources``) that the native path passes
to the real ``claude`` binary.
"""

from __future__ import annotations

import json
from pathlib import Path


def ensure_bundle_plugin_manifest(
    bundle_dir: Path,
    agent_name: str | None,
) -> None:
    """
    Write a minimal ``<bundle>/.claude-plugin/plugin.json`` manifest
    when one isn't already present.

    Idempotent — if the file already exists (including with a
    user-supplied richer manifest), it's left untouched. The
    manifest gives the bundle a stable plugin name so Claude's
    skill listing labels bundled skills as
    ``<agent-name>:<skill-name>`` instead of falling back to the
    bundle's auto-generated tmp-dir basename
    (e.g. ``omnigent-ap-chat-x9p606iz/bundle:researcher``).

    :param bundle_dir: Materialized bundle root; the manifest is
        written at ``<bundle_dir>/.claude-plugin/plugin.json``.
    :param agent_name: Display name for the plugin. ``None`` falls
        back to the bundle directory's basename — still
        deterministic, just less readable.
    :returns: None.
    """
    manifest_dir = bundle_dir / ".claude-plugin"
    manifest_path = manifest_dir / "plugin.json"
    if manifest_path.exists():
        return
    manifest_dir.mkdir(parents=True, exist_ok=True)
    name = agent_name or bundle_dir.name
    manifest_path.write_text(
        json.dumps(
            {
                "name": name,
                "description": f"Bundled skills for omnigent agent {name!r}",
            },
            indent=2,
        )
        + "\n",
    )
