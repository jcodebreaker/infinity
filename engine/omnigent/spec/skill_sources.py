"""
Harness-aware host/plugin skill discovery for the web composer's
slash-command menu.

The host previews skills before launch; the runner unions the same
discovery results with a session's bundled skills.
"What a harness exposes" differs per vendor (Claude Code plugins, Pi's own
runtime discovery), so resolution dispatches to a per-family provider.
Unknown harnesses fall back to the generic host walk
(``discover_host_skills``) for backwards compatibility.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from omnigent.spec.parser import _discover_skills, discover_host_skills
from omnigent.spec.types import AgentSpec, SkillSpec

_log = logging.getLogger(__name__)

_SKILL_FAMILIES = frozenset({"claude", "pi"})


def _harness_family(harness: str | None) -> str | None:
    """
    Map any harness spelling to its vendor family, or ``None``.

    Collapses variant suffixes — ``claude-sdk`` and ``claude_sdk`` both map
    to ``"claude"`` — so the provider registry can be keyed by family rather
    than by every canonical variant.

    :param harness: A harness id (canonical or alias), e.g.
        ``"claude-sdk"``; ``None`` or ``""`` returns ``None``.
    :returns: One of ``"claude"``, ``"pi"``, or ``None`` for unknown/other harnesses.
    """
    if not harness:
        return None
    # Normalize the underscore executor-type spelling (``claude_sdk``,
    # ``agents_sdk``) to the hyphen form before splitting — the in-process
    # SDK harness flows in as ``claude_sdk`` (canonicalize_harness leaves it
    # unchanged), and without this it would miss the ``claude`` family and
    # silently lose plugin slash-commands.
    normalized = harness.replace("_", "-")
    base = normalized.split("-")[0]
    return base if base in _SKILL_FAMILIES else None


@dataclass(frozen=True)
class SkillSourceContext:
    """
    Inputs a per-harness skill provider needs.

    :param roots: Discovery roots in priority order — the selected host
        directory, or the session workspace followed by the agent bundle workdir.
    :param home: The user home directory (``Path.home()``); injected so
        tests can pin it.
    :param skills_filter: The spec's ``skills:`` filter
        (``"all"`` / ``"none"`` / list of names).
    :param bundle_dir: The materialized bundle root, or ``None``.
    :param claude_config_dir: Claude Code's user config dir when configured
        (``$CLAUDE_CONFIG_DIR``), else ``None`` for the ``home/.claude``
        default; injected so tests can pin it.
    :param use_claude_config_dir: Resolve Claude's user dir through
        *claude_config_dir* (as Claude Code itself does) rather than the
        ``home/.claude`` root the in-process SDK harness uses.
    """

    roots: tuple[Path, ...]
    home: Path
    skills_filter: str | list[str]
    bundle_dir: Path | None
    claude_config_dir: Path | None = None
    use_claude_config_dir: bool = False


SkillSource = Callable[[SkillSourceContext], list[SkillSpec]]


def skill_source_context_from_env(
    *,
    roots: tuple[Path, ...],
    skills_filter: str | list[str] = "all",
    bundle_dir: Path | None = None,
) -> SkillSourceContext:
    """Use the same ambient harness configuration for host and runner discovery."""
    configured_claude_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return SkillSourceContext(
        roots=roots,
        home=Path.home(),
        skills_filter=skills_filter,
        bundle_dir=bundle_dir,
        claude_config_dir=(
            Path(configured_claude_dir).expanduser() if configured_claude_dir else None
        ),
    )


def _dedup(specs: list[SkillSpec]) -> list[SkillSpec]:
    """Return *specs* with later same-name entries dropped (first wins)."""
    seen: set[str] = set()
    out: list[SkillSpec] = []
    for s in specs:
        if s.name in seen:
            continue
        seen.add(s.name)
        out.append(s)
    return out


def _generic_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """Today's behavior: ``discover_host_skills`` over each root."""
    out: list[SkillSpec] = []
    for root in ctx.roots:
        out.extend(discover_host_skills(root, ctx.skills_filter))
    return _dedup(out)


def _claude_user_dir(ctx: SkillSourceContext) -> Path:
    """
    Claude Code's user-scope config dir (its skills/plugins/settings root).

    With ``use_claude_config_dir`` this mirrors Claude's own resolution
    (``CLAUDE_CONFIG_DIR`` when set, otherwise ``~/.claude``). The in-process
    SDK path keeps its ``~/.claude`` root.
    """
    if ctx.use_claude_config_dir and ctx.claude_config_dir is not None:
        return ctx.claude_config_dir
    return ctx.home / ".claude"


def resolve_harness_skills(ctx: SkillSourceContext, harness: str | None) -> list[SkillSpec]:
    """
    Return the extra (non-bundled) skills the selected harness exposes.

    Dispatches by harness family. An unknown/other family falls back to
    the generic host walk so behavior is unchanged for harnesses without
    a dedicated provider.

    :param ctx: Session discovery context.
    :param harness: The session's harness id (canonical or alias).
    :returns: Deduplicated skill specs (first occurrence wins).
        Skills marked ``user-invocable: false`` are excluded — they are
        internal orchestration skills, not user-typeable slash commands
        (applied uniformly across every harness).
    """
    family = _harness_family(harness)
    provider = _SKILL_SOURCES.get(family, _generic_host_skills)
    return [s for s in _dedup(provider(ctx)) if s.user_invocable]


def resolve_session_skills(
    spec: AgentSpec, roots: tuple[Path, ...], bundle_dir: Path | None
) -> list[SkillSpec]:
    """Use identical precedence and filters for menu discovery and invocation."""
    from omnigent.harness_aliases import canonicalize_harness

    merged = [s for s in spec.skills if s.user_invocable]
    seen = {s.name for s in spec.skills}
    seen_dirs = {s.skill_dir.resolve() for s in spec.skills if s.skill_dir is not None}
    harness = canonicalize_harness(spec.executor.harness_kind)
    ctx = skill_source_context_from_env(
        roots=roots, skills_filter=spec.skills_filter, bundle_dir=bundle_dir
    )
    for skill in resolve_harness_skills(ctx, harness):
        if skill.name in seen:
            continue
        if skill.skill_dir is not None and skill.skill_dir.resolve() in seen_dirs:
            continue
        seen.add(skill.name)
        if skill.skill_dir is not None:
            seen_dirs.add(skill.skill_dir.resolve())
        merged.append(skill)
    return merged


def _read_json(path: Path) -> dict[str, object] | None:
    """Best-effort JSON read; ``None`` unless it is a string-keyed object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        return None
    return cast(dict[str, object], data)


def _enabled_plugin_settings_files(ctx: SkillSourceContext) -> list[Path]:
    """
    Settings files carrying ``enabledPlugins``, weakest→strongest.

    Claude Code merges ``enabledPlugins`` across its settings tiers, and
    the gitignored ``settings.local.json`` (the "local" tier) wins over
    the shared ``settings.json`` within a scope. Reading only
    ``settings.json`` would miss a plugin toggled in the local file. We
    approximate Claude's precedence for the menu (last-wins):

        user (home) .json → .local  <  bundle .json → .local
            <  workspace .json → .local

    — ``.local`` over ``.json`` within each scope, project (roots) over
    user (home), and the primary workspace (``roots[0]``) over the shipped
    bundle (``roots[1]``), matching the workspace-first precedence the host
    skill walk already uses.

    :param ctx: Session discovery context.
    :returns: Candidate settings paths in increasing precedence order.
    """
    files: list[Path] = []
    # User scope first (weakest): $CLAUDE_CONFIG_DIR itself carries the
    # settings files (default ~/.claude). ``reversed`` then puts the primary
    # workspace (roots[0]) last → strongest.
    user_dir = _claude_user_dir(ctx)
    files.append(user_dir / "settings.json")
    files.append(user_dir / "settings.local.json")
    for scope in reversed(ctx.roots):
        files.append(scope / ".claude" / "settings.json")
        files.append(scope / ".claude" / "settings.local.json")
    return files


def _managed_plugin_keys(ctx: SkillSourceContext) -> set[str]:
    """
    Plugin keys force-enabled by Claude Code's managed (policy) tier.

    ``~/.claude/plugins/managed_plugins.json`` carries a ``managed_plugins``
    list of ``<plugin>@<marketplace>`` keys that an organization's policy
    force-installs. Claude Code treats the managed tier as the highest
    precedence — it cannot be overridden by a user/project ``enabledPlugins``
    toggle — so a managed plugin is enabled even when it carries no
    ``enabledPlugins`` entry (or an explicit ``false``) in any settings file.

    :param ctx: Session discovery context.
    :returns: The set of managed ``<plugin>@<marketplace>`` keys, empty when
        the file is absent, unreadable, or malformed.
    """
    data = _read_json(_claude_user_dir(ctx) / "plugins" / "managed_plugins.json")
    if data is None:
        return set()
    managed = data.get("managed_plugins")
    if not isinstance(managed, list):
        return set()
    return {key for key in managed if isinstance(key, str) and key}


def _enabled_plugin_keys(ctx: SkillSourceContext) -> set[str]:
    """
    Resolve which plugins are enabled, honoring scope + local precedence
    and the managed (policy) tier.

    Merges ``enabledPlugins`` across the settings files from
    :func:`_enabled_plugin_settings_files` in increasing-precedence order
    (last-wins per key), so a plugin enabled globally but explicitly
    disabled in a project's ``settings.local.json`` is correctly excluded
    (and vice-versa). The managed tier (:func:`_managed_plugin_keys`) is then
    unioned on top: it force-enables at the highest precedence and so cannot
    be overridden by a settings disable.

    :param ctx: Session discovery context.
    :returns: The set of ``<plugin>@<marketplace>`` keys whose effective
        enablement is truthy, unioned with the managed (policy) keys.
    """
    state: dict[str, bool] = {}
    for path in _enabled_plugin_settings_files(ctx):
        data = _read_json(path)
        if data is None:
            continue
        ep = data.get("enabledPlugins")
        if isinstance(ep, dict):
            for key, value in ep.items():
                # Accept only a real JSON boolean — a string ``"false"`` is
                # truthy under ``bool()`` and would wrongly enable a plugin.
                # Non-bool values are ignored (they don't override a prior
                # real bool from a weaker-precedence tier).
                if isinstance(value, bool):
                    state[key] = value
    enabled = {key for key, on in state.items() if on}
    # Managed (policy) plugins are force-enabled at the highest precedence and
    # cannot be overridden by a user/project enabledPlugins toggle, so union
    # them on top of the settings-derived set (force-enable, never disable).
    return enabled | _managed_plugin_keys(ctx)


def _plugin_install_paths(ctx: SkillSourceContext, enabled: set[str]) -> dict[str, Path]:
    """Map ``<plugin>@<marketplace>`` → installPath for enabled+installed plugins."""
    data = _read_json(_claude_user_dir(ctx) / "plugins" / "installed_plugins.json")
    if data is None:
        return {}
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    # installPath values from installed_plugins.json are trusted only within
    # the Claude plugins cache root; anything pointing elsewhere is logged and
    # skipped so a tampered/odd manifest can't turn an arbitrary directory into
    # a discovery root.
    plugins_root = (_claude_user_dir(ctx) / "plugins").resolve()
    out: dict[str, Path] = {}
    for key, entries in plugins.items():
        if key not in enabled or not isinstance(entries, list):
            continue
        # A plugin may have multiple scope entries (user/project); take the
        # first one carrying a usable installPath rather than assuming it's
        # entries[0], whose scope may not have an installed path.
        for entry in entries:
            path = entry.get("installPath") if isinstance(entry, dict) else None
            if not (isinstance(path, str) and path):
                continue
            # A relative installPath is resolved against the plugins root
            # (its only sensible base), never the runner's cwd.
            path_obj = Path(path)
            resolved = (path_obj if path_obj.is_absolute() else plugins_root / path_obj).resolve()
            if not resolved.is_relative_to(plugins_root):
                _log.warning(
                    "Skipping plugin %r: installPath %r is outside %s",
                    key,
                    path,
                    plugins_root,
                )
                break
            out[key] = resolved
            break
    return out


def _claude_plugin_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """
    Enabled Claude Code plugin skills, namespaced ``<plugin>:<skill>``.

    Plugin skills are host skills, so they obey the spec's
    ``skills_filter`` exactly as :func:`discover_host_skills` does:
    ``"none"`` suppresses them entirely (hermetic), ``"all"`` surfaces
    every skill from every enabled plugin, and a list selects by the
    skill's own (bare) name — matching how the filter names skills,
    independent of the display namespace.
    """
    if ctx.skills_filter == "none":
        return []
    filter_names: set[str] | None = (
        set(ctx.skills_filter) if isinstance(ctx.skills_filter, list) else None
    )
    enabled = _enabled_plugin_keys(ctx)
    if not enabled:
        return []
    out: list[SkillSpec] = []
    for key, install_path in _plugin_install_paths(ctx, enabled).items():
        plugin = key.split("@", 1)[0]
        skipped: list[str] = []
        for spec in _discover_skills(install_path / "skills", skipped=skipped):
            if filter_names is not None and spec.name not in filter_names:
                continue
            out.append(replace(spec, name=f"{plugin}:{spec.name}"))
        # Surface dropped skills with the plugin key so a missing command is
        # diagnosable rather than silently absent.
        for detail in skipped:
            _log.warning("Plugin %r: skipped skill: %s", key, detail)
    return out


def claude_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """Claude host skills: the generic host walk plus enabled plugin skills."""
    return _generic_host_skills(ctx) + _claude_plugin_skills(ctx)


def pi_host_skills(ctx: SkillSourceContext) -> list[SkillSpec]:
    """
    Pi exposes no *extra* discoverable skills to the menu.

    Pi has its **own** host-skill mechanism: it loads bundle skills via
    ``--skill`` (already carried by ``spec.skills``, which the runner
    unions in separately) and runs its own auto-discovery at runtime,
    sourcing from Pi's internal extension layout. omnigent can't enumerate
    that layout to name/resolve those skills for the menu, so listing any
    would risk surfacing a command Pi can't invoke. Hence the explicit
    no-op (under-report when unsure).

    This is why Pi gets a dedicated provider rather than the generic
    ``~/.claude/skills`` fallback: the fallback's skills are resolvable by
    omnigent on harnesses that have *no* host-skill mechanism of their own
    (which route a typed skill through omnigent's own resolve+inject), but
    Pi's competing,
    unenumerable mechanism makes that fallback unsafe. The distinction is
    "does the harness own an unenumerable host-skill mechanism", not the
    vendor.

    :param ctx: Session discovery context (unused).
    :returns: Always an empty list.
    """
    del ctx
    return []


# Keyed by harness family (see _harness_family). A harness with no entry
# falls through to _generic_host_skills in resolve_harness_skills — the ~/.claude/skills walk,
# whose skills omnigent resolves+injects regardless of vendor.
#
# The dividing line is whether the harness owns a host-skill mechanism, and if
# so whether omnigent can enumerate it:
#
#   no mechanism            -> generic walk (omnigent injects the skill text)
#   mechanism, enumerable   -> dedicated provider listing what the agent has
#                              (claude)
#   mechanism, unenumerable -> explicit no-op (pi) — listing anything would
#                              risk surfacing a command the harness can't run
_SKILL_SOURCES: dict[str | None, SkillSource] = {
    "claude": claude_host_skills,
    "pi": pi_host_skills,
}
