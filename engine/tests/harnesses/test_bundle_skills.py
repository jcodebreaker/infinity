"""
Tests for ``omnigent.harnesses.bundle_skills`` — the shared helpers that
expose an agent bundle's skills to a Claude harness (the SDK executor and
the ``claude-native`` CLI launch path both use these so they stay in
lockstep).
"""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.harnesses.bundle_skills import (
    ensure_bundle_plugin_manifest,
)


def test_ensure_bundle_plugin_manifest_writes_when_missing(tmp_path: Path) -> None:
    """
    With no ``.claude-plugin/plugin.json`` present, the helper writes one
    with ``name = agent_name``.

    A regression that wrote the wrong name (or the bundle's basename)
    would mis-namespace every bundled skill in the model's listing
    (e.g. ``omnigent-ap-chat-x9p606iz/bundle:researcher``).
    """
    ensure_bundle_plugin_manifest(tmp_path, "coding-supervisor")
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    assert manifest.is_file()
    assert json.loads(manifest.read_text())["name"] == "coding-supervisor"


def test_ensure_bundle_plugin_manifest_is_idempotent(tmp_path: Path) -> None:
    """
    An existing manifest is preserved verbatim — the helper bails on the
    first ``.exists()`` check, protecting a user-authored richer manifest
    (e.g. with ``version`` / ``author``) from being overwritten.
    """
    (tmp_path / ".claude-plugin").mkdir()
    existing = tmp_path / ".claude-plugin" / "plugin.json"
    user_authored = '{"name":"user-name","author":"someone"}'
    existing.write_text(user_authored)
    ensure_bundle_plugin_manifest(tmp_path, "different-name")
    # Unchanged bytes — a regression that overwrote unconditionally would
    # silently drop the user's metadata.
    assert existing.read_text() == user_authored


def test_ensure_bundle_plugin_manifest_falls_back_to_basename(tmp_path: Path) -> None:
    """
    When ``agent_name`` is ``None`` the manifest name falls back to the
    bundle directory's basename — still deterministic, just less readable.
    """
    bundle = tmp_path / "my-bundle"
    bundle.mkdir()
    ensure_bundle_plugin_manifest(bundle, None)
    data = json.loads((bundle / ".claude-plugin" / "plugin.json").read_text())
    assert data["name"] == "my-bundle"


def _make_bundle_with_skill(root: Path) -> Path:
    """
    Create a minimal bundle dir containing one skill.

    :param root: Parent dir to create the bundle under.
    :returns: The bundle root path (contains ``skills/only/SKILL.md``).
    """
    bundle = root / "bundle"
    (bundle / "skills" / "only").mkdir(parents=True)
    (bundle / "skills" / "only" / "SKILL.md").write_text("# only\n")
    return bundle
