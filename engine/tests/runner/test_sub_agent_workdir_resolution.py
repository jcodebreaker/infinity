"""Tests for sub-agent bundle-workdir resolution.

A sub-agent's skills and local tools live in its own bundle directory,
``<parent bundle>/agents/<dir>``. Resolving a sub-agent by name used to
hand back the PARENT's bundle root as the child's workdir, so the child
booted with the parent's skills and local tools loaded — a privilege
leak across the agent boundary.

``_resolve_sub_agent_spec_entry`` walks the identity chain from the
parent spec down to the child, composing one ``agents/<dir>`` hop per
level from each spec's ``source_rel_dir``. Anything it cannot prove
(unreachable spec, unsafe segment, directory absent on disk) yields
``workdir=None`` — the child boots with no bundle assets rather than
with the parent's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.runner.spec_entries import (
    ResolvedSpec,
    _resolve_sub_agent_spec_entry,
    _resolved_workdir_for_spec,
)
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec


def _write_agent(directory: Path, name: str) -> Path:
    """Create a minimal agent bundle at *directory* named *name*."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(yaml.dump({"spec_version": 1, "name": name}))
    return directory


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    """A three-level bundle: root → manager → reviewer."""
    root = _write_agent(tmp_path / "root", "root")
    manager = _write_agent(root / "agents" / "manager", "manager")
    _write_agent(manager / "agents" / "reviewer", "reviewer")
    return root


def test_direct_child_resolves_to_its_own_bundle_dir(bundle: Path) -> None:
    """A direct sub-agent gets ``<parent>/agents/<dir>``, not the parent root."""
    entry = ResolvedSpec(spec=parse(bundle), workdir=bundle)

    resolved = _resolve_sub_agent_spec_entry(entry, "manager")

    assert resolved is not None
    assert resolved.workdir == bundle / "agents" / "manager"
    assert resolved.spec.name == "manager"


def test_grandchild_resolves_through_the_full_chain(bundle: Path) -> None:
    """A nested sub-agent composes one ``agents/<dir>`` hop per level.

    Regression: resolving only the last hop (or none at all) would root
    the grandchild at the parent bundle and re-expose the whole tree's
    assets to it.
    """
    entry = ResolvedSpec(spec=parse(bundle), workdir=bundle)

    resolved = _resolve_sub_agent_spec_entry(entry, "reviewer")

    assert resolved is not None
    assert resolved.workdir == bundle / "agents" / "manager" / "agents" / "reviewer"
    assert resolved.spec.name == "reviewer"


def test_resolution_always_wraps_never_substitutes_the_parent(bundle: Path) -> None:
    """A hit returns a wrapped child entry — never the parent entry.

    The wrapper is what makes downstream workdir lookups honour a
    ``None`` instead of silently falling back to the runner workspace or
    the parent bundle.
    """
    parent_entry = ResolvedSpec(spec=parse(bundle), workdir=bundle)

    resolved = _resolve_sub_agent_spec_entry(parent_entry, "manager")

    assert isinstance(resolved, ResolvedSpec)
    assert resolved is not parent_entry
    assert resolved.spec is not parent_entry.spec


def test_resolving_an_already_resolved_child_is_a_silent_no_op(
    bundle: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The session cache holds the child entry; resolving it again must not miss.

    Regression: the tree search never matches its own root, so re-resolving the cached
    child logged "Sub-agent 'x' not found under spec 'x'" on every turn.
    """
    parent_entry = ResolvedSpec(spec=parse(bundle), workdir=bundle)
    child = _resolve_sub_agent_spec_entry(parent_entry, "manager")
    assert child is not None

    with caplog.at_level("WARNING"):
        again = _resolve_sub_agent_spec_entry(child, "manager")

    assert again is child
    assert "not found under spec" not in caplog.text


def test_unknown_sub_agent_name_returns_none(bundle: Path) -> None:
    """A lookup miss yields ``None`` so callers can decline to swap."""
    entry = ResolvedSpec(spec=parse(bundle), workdir=bundle)

    assert _resolve_sub_agent_spec_entry(entry, "nope") is None


def test_dotted_directory_name_is_accepted(tmp_path: Path) -> None:
    """``review..worker`` is a legitimate single path component.

    The segment guard is a component check, not a substring reject —
    rejecting anything containing ``..`` would break real directories
    and silently drop those children back to a ``None`` workdir.
    """
    root = _write_agent(tmp_path / "root", "root")
    _write_agent(root / "agents" / "review..worker", "reviewer")
    entry = ResolvedSpec(spec=parse(root), workdir=root)

    resolved = _resolve_sub_agent_spec_entry(entry, "reviewer")

    assert resolved is not None
    assert resolved.workdir == root / "agents" / "review..worker"


@pytest.mark.parametrize("segment", [".", "..", "/etc", "a/b", "", None])
def test_unsafe_segments_are_rejected(bundle: Path, segment: str | None) -> None:
    """Traversal / absolute / multi-component segments yield no workdir.

    A crafted bundle that smuggled ``..`` or an absolute path into the
    provenance stamp could otherwise point a child's plugin dir at an
    arbitrary directory on the runner host.
    """
    root_spec = parse(bundle)
    (child,) = root_spec.sub_agents
    child.source_rel_dir = segment  # type: ignore[assignment]
    entry = ResolvedSpec(spec=root_spec, workdir=bundle)

    resolved = _resolve_sub_agent_spec_entry(entry, "manager")

    assert resolved is not None
    assert resolved.workdir is None


def test_missing_directory_on_disk_yields_none(bundle: Path) -> None:
    """The composed path must actually exist, or the child gets nothing."""
    root_spec = parse(bundle)
    (child,) = root_spec.sub_agents
    child.source_rel_dir = "moved-away"
    entry = ResolvedSpec(spec=root_spec, workdir=bundle)

    resolved = _resolve_sub_agent_spec_entry(entry, "manager")

    assert resolved is not None
    assert resolved.workdir is None


def test_parent_without_workdir_yields_child_without_workdir(bundle: Path) -> None:
    """No parent bundle root means nothing to resolve the child against."""
    entry = ResolvedSpec(spec=parse(bundle), workdir=None)

    resolved = _resolve_sub_agent_spec_entry(entry, "manager")

    assert resolved is not None
    assert resolved.workdir is None


def test_bare_parent_spec_yields_child_without_workdir(bundle: Path) -> None:
    """An unwrapped parent spec carries no workdir, so neither does the child."""
    resolved = _resolve_sub_agent_spec_entry(parse(bundle), "manager")

    assert resolved is not None
    assert resolved.workdir is None


# ── _resolved_workdir_for_spec ────────────────────────────────


def test_wrapped_entry_workdir_wins_over_fallback(tmp_path: Path) -> None:
    """A wrapped entry's workdir is returned verbatim."""
    entry = ResolvedSpec(spec=AgentSpec(spec_version=1, name="probe"), workdir=tmp_path)

    assert _resolved_workdir_for_spec(entry, Path("/fallback")) == tmp_path


def test_wrapped_entry_with_none_workdir_does_not_fall_back() -> None:
    """``workdir=None`` on a wrapped entry means "no bundle", not "guess".

    This is the leak fix: falling back to the runner workspace here would
    put a child agent back onto a directory it has no claim to.
    """
    entry = ResolvedSpec(spec=AgentSpec(spec_version=1, name="probe"), workdir=None)

    assert _resolved_workdir_for_spec(entry, Path("/fallback")) is None


def test_bare_spec_still_uses_the_fallback() -> None:
    """Unwrapped specs keep the pre-existing fallback behaviour."""
    spec = AgentSpec(spec_version=1, name="probe")

    assert _resolved_workdir_for_spec(spec, Path("/fallback")) == Path("/fallback")
