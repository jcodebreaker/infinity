"""Agent-spec cache entries that carry their bundle workdir.

The runner caches each session's resolved :class:`AgentSpec`. A spec loaded
from a bundle is wrapped in :class:`ResolvedSpec` so downstream lookups see
its own bundle directory (a sub-agent's ``agents/<dir>``, or ``None``) rather
than widening to the runner workspace.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

from omnigent.debug_logging import runner_primary_session_id
from omnigent.spec.types import AgentSpec

_logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ResolvedSpec:
    spec: AgentSpec
    workdir: Path | None

    def __getattr__(self, name: str) -> Any:  # type: ignore[explicit-any]
        """Delegate compatibility attributes to the wrapped agent spec."""
        return getattr(self.spec, name)


def _unwrap_resolved_spec(entry: object) -> Any:  # type: ignore[explicit-any]
    """Unwrap cached specs without rejecting legacy or test resolver objects."""
    return entry.spec if isinstance(entry, ResolvedSpec) else entry


def _is_safe_bundle_segment(segment: Any) -> bool:
    """Return whether *segment* is a single, relative path component.

    Component check, not substring rejection: a directory legitimately
    named ``review..worker`` is one component and stays valid, while
    ``..``, ``a/b`` and absolute paths do not.
    """
    if not isinstance(segment, str) or not segment or segment in (".", ".."):
        return False
    try:
        candidate = Path(segment)
    except (TypeError, ValueError):
        return False
    return candidate.parts == (segment,) and not candidate.is_absolute()


def _sub_agent_bundle_segments(root: Any, child: Any) -> list[str] | None:
    """Identity-walk *child* up to *root*, collecting its bundle dir names.

    Returns ``None`` when the child is not reachable from the root by
    identity (synthetic / in-memory specs)
    or when any hop lacks a usable ``source_rel_dir``.
    """
    parents: dict[int, Any] = {}
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        for sub in getattr(node, "sub_agents", None) or []:
            parents[id(sub)] = node
            stack.append(sub)
    segments: list[str] = []
    current = child
    while current is not root:
        parent = parents.get(id(current))
        if parent is None:
            return None
        segment = getattr(current, "source_rel_dir", None)
        if not _is_safe_bundle_segment(segment):
            return None
        segments.append(str(segment))
        current = parent
    segments.reverse()
    return segments


def _sub_agent_bundle_workdir(parent_entry: Any, parent_spec: Any, child_spec: Any) -> Path | None:
    """Compose ``<parent workdir>/agents/<dir>`` per hop down to *child_spec*."""
    parent_workdir = _resolved_spec_workdir(parent_entry)
    if parent_workdir is None:
        return None
    segments = _sub_agent_bundle_segments(parent_spec, child_spec)
    if segments is None:
        return None
    workdir = parent_workdir
    for segment in segments:
        workdir = workdir / "agents" / segment
    return workdir if workdir.is_dir() else None


def _resolve_sub_agent_spec_entry(parent_entry: Any, sub_agent_name: str) -> ResolvedSpec | None:
    """Resolve a sub-agent by name into a spec entry rooted at its own bundle dir.

    The returned entry is always wrapped so downstream workdir lookups see
    the child's own directory (or ``None``) — never the parent's bundle
    root, which would expose the parent's skills and local tools to the
    child.

    :param parent_entry: Parent spec, bare or wrapped in ``ResolvedSpec``.
    :param sub_agent_name: Name of the sub-agent to resolve.
    :returns: The wrapped child entry, or ``None`` when the name does not
        resolve.
    """
    from omnigent.runtime.workflow import _find_spec_by_name

    parent_spec = _unwrap_resolved_spec(parent_entry)
    if (
        isinstance(parent_entry, ResolvedSpec)
        and getattr(parent_spec, "name", None) == sub_agent_name
    ):
        # Already the child: the session cache holds the resolved entry, and sub-agent
        # names are unique across the tree, so resolving again is a no-op. (The tree
        # search never matches its own root, so this would otherwise be a spurious miss.)
        return parent_entry
    child_spec = _find_spec_by_name(parent_spec, sub_agent_name)
    if child_spec is None:
        # Callers in runtime/workflow.py keep the parent spec on a lookup
        # miss, which boots the child as a clone of the parent. Unsafe, but
        # pre-existing and out of scope here — tracked separately.
        _logger.warning(
            "Sub-agent %r not found under spec %r; no workdir resolved",
            sub_agent_name,
            getattr(parent_spec, "name", None),
            extra={"session_id": runner_primary_session_id()},
        )
        return None
    return ResolvedSpec(
        spec=child_spec,
        workdir=_sub_agent_bundle_workdir(parent_entry, parent_spec, child_spec),
    )


def _resolved_spec_workdir(entry: object) -> Path | None:
    return entry.workdir if isinstance(entry, ResolvedSpec) else None


def _resolved_workdir_for_spec(
    spec: object,
    fallback: Path | None,
) -> Path | None:
    """Return the bundle workdir for a possibly wrapped spec entry."""
    if isinstance(spec, ResolvedSpec):
        return spec.workdir
    return fallback


def _rewrap_like(previous: object, spec: AgentSpec, workdir: Path | None) -> Any:  # type: ignore[explicit-any]
    """Re-wrap *spec* iff *previous* was wrapped, preserving a ``None`` workdir.

    A wrapped entry has a verdict on its bundle dir — including a sub-agent's
    ``None`` — and re-widening that to the runner workspace is the leak. A spec
    that never carried bundle information stays bare so it keeps the workspace
    fallback ordinary top-level sessions rely on.
    """
    return ResolvedSpec(spec=spec, workdir=workdir) if isinstance(previous, ResolvedSpec) else spec


def _is_spec_local_native_python_tool(
    spec: object,
    tool_name: str,
) -> bool:
    """Return whether *tool_name* is a spec-declared native python tool."""
    unwrapped = _unwrap_resolved_spec(spec)
    if unwrapped is None:
        return False
    return any(
        getattr(info, "name", None) == tool_name
        and getattr(info, "language", None) in ("python", "omnigent-python-callable")
        for info in getattr(unwrapped, "local_tools", [])
    )
