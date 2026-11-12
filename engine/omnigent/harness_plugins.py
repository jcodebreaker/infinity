"""Dynamic harness contribution registry.

Core Omnigent contributes the built-in harnesses directly. Optional community
packages contribute additional harnesses through the
``omnigent.community.harness`` entry point group.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar, cast

from omnigent.harness_capabilities import (
    AuthModel,
    EffortFamily,
    Elicitation,
    ForkHistory,
    HarnessCapabilities,
    InstructionDelivery,
    IntegrationMode,
    ModelFamily,
    Resume,
)
from omnigent.harness_install_spec import HarnessInstallSpec

_logger = logging.getLogger(__name__)

COMMUNITY_ENTRY_POINT_GROUP = "omnigent.community.harness"
COMMUNITY_MODULE_PREFIX = "omnigent.community.harness."


@dataclass(frozen=True)
class HarnessContribution:
    """One package's harness registry contribution."""

    name: str
    valid_harnesses: frozenset[str] = frozenset()
    harness_modules: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    install_specs: dict[str, HarnessInstallSpec] = field(default_factory=dict)
    harness_install_keys: dict[str, str] = field(default_factory=dict)
    model_env_keys: dict[str, str] = field(default_factory=dict)
    spawn_env_builders: dict[str, str] = field(default_factory=dict)
    missing_install_package: dict[str, str] = field(default_factory=dict)
    harness_labels: dict[str, str] = field(default_factory=dict)
    # Declared feature set per harness id ("what can this harness do?"). Sparse
    # is allowed — a harness with no entry simply has no declared capabilities.
    capabilities: dict[str, HarnessCapabilities] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessPluginState:
    """Merged harness registry plus non-fatal plugin load errors."""

    contributions: tuple[HarnessContribution, ...]
    load_errors: dict[str, str]


# Declared capabilities for the built-in harnesses. Each value is backed by the
# module that implements it; the derivable axes (model_family, subagents) are
# asserted against their source in tests/test_harness_capabilities.py so the
# table cannot silently drift. See designs/harness-modular-registry-proposal.md.
_C = HarnessCapabilities
_IM = IntegrationMode
_EL = Elicitation
_RS = Resume
_EF = EffortFamily
_MF = ModelFamily
_AU = AuthModel
_FH = ForkHistory
_ID = InstructionDelivery

# Bench shell-tool provocation prompt for harnesses whose exec tool is literally
# "Bash". Keeps the "omnigent-bench-ok" placeholder the bench token-swaps per
# allow/deny probe.
_BASH_PROMPT = "Use the Bash tool to run this exact command: echo omnigent-bench-ok"

# Trailing two bools are (interrupt, streaming), verified live by the harness
# bench for claude-sdk and pi.
_BUILTIN_CAPABILITIES: dict[str, HarnessCapabilities] = {
    # SDK / subprocess harnesses (run the vendor model directly).
    "claude-sdk": _C(
        _IM.SDK_IN_PROCESS,
        _EL.NONE,
        _RS.COLD_ONLY,
        _EF.ANTHROPIC,
        _MF.CLAUDE,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=False,
        interrupt=True,
        streaming=True,
        instruction_delivery=_ID.COMPOSED_SESSION_SNAPSHOT,
    ),
    "pi": _C(
        _IM.CLI_SUBPROCESS,
        _EL.NONE,
        _RS.COLD_ONLY,
        _EF.PI,
        _MF.MULTI,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=False,
        interrupt=True,
        streaming=True,
        instruction_delivery=_ID.COMPOSED_PER_TURN,
    ),
}


_BUILTIN_CONTRIBUTION = HarnessContribution(
    name="omnigent",
    valid_harnesses=frozenset({"claude-sdk", "pi"}),
    harness_modules={
        "claude-sdk": "omnigent.harnesses.claude_sdk_harness",
        "pi": "omnigent.harnesses.pi_harness",
    },
    aliases={
        "claude": "claude-sdk",
    },
    model_env_keys={
        "claude-sdk": "HARNESS_CLAUDE_SDK_MODEL",
        "pi": "HARNESS_PI_MODEL",
    },
    harness_labels={
        "claude-sdk": "Claude SDK",
        "pi": "Pi",
    },
    capabilities=_BUILTIN_CAPABILITIES,
)

_state: HarnessPluginState | None = None


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    discovered = importlib.metadata.entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=COMMUNITY_ENTRY_POINT_GROUP))
    legacy = cast(
        Mapping[str, Iterable[importlib.metadata.EntryPoint]],
        discovered,
    )
    return tuple(legacy.get(COMMUNITY_ENTRY_POINT_GROUP, ()))


def _module_part(import_path: str) -> str:
    return import_path.split(":", 1)[0]


def _community_paths(contribution: HarnessContribution) -> list[str]:
    paths: list[str] = []
    paths.extend(contribution.harness_modules.values())
    paths.extend(contribution.spawn_env_builders.values())
    return paths


def _harness_spellings(contribution: HarnessContribution) -> set[str]:
    """Return every harness/alias key claimed by a contribution."""
    return (
        set(contribution.valid_harnesses)
        | set(contribution.harness_modules)
        | set(contribution.aliases)
        | set(contribution.harness_install_keys)
        | set(contribution.model_env_keys)
        | set(contribution.missing_install_package)
        | set(contribution.harness_labels)
        | set(contribution.capabilities)
    )


def _validate_community_contribution(
    contribution: HarnessContribution,
    *,
    entry_point_name: str,
    existing: tuple[HarnessContribution, ...],
) -> str | None:
    if not contribution.name:
        return "plugin contribution must set name"

    for path in _community_paths(contribution):
        if not _module_part(path).startswith(COMMUNITY_MODULE_PREFIX):
            return (
                f"community harness plugin {entry_point_name!r} uses import path "
                f"{path!r}; expected {COMMUNITY_MODULE_PREFIX}*"
            )

    existing_harness_spellings: set[str] = set()
    existing_install_keys: set[str] = set()
    for accepted in existing:
        existing_harness_spellings.update(_harness_spellings(accepted))
        existing_install_keys.update(accepted.install_specs)

    harness_collisions = existing_harness_spellings.intersection(_harness_spellings(contribution))
    if harness_collisions:
        return (
            f"community harness plugin {entry_point_name!r} attempts to override "
            f"existing harness keys: {sorted(harness_collisions)}"
        )

    install_key_collisions = existing_install_keys.intersection(contribution.install_specs)
    if install_key_collisions:
        return (
            f"community harness plugin {entry_point_name!r} attempts to override "
            f"existing install keys: {sorted(install_key_collisions)}"
        )

    allowed_targets = set(contribution.valid_harnesses)
    for alias, target in contribution.aliases.items():
        if target not in allowed_targets:
            return (
                f"community harness plugin {entry_point_name!r} alias {alias!r} "
                f"targets {target!r}, which is not contributed by the plugin"
            )
    return None


def plugin_state() -> HarnessPluginState:
    """Return the merged built-in + community harness registry."""
    global _state
    if _state is not None:
        return _state

    contributions: list[HarnessContribution] = [_BUILTIN_CONTRIBUTION]
    load_errors: dict[str, str] = {}
    for entry_point in _entry_points():
        try:
            loaded = entry_point.load()
            contribution = loaded() if callable(loaded) else loaded
            if not isinstance(contribution, HarnessContribution):
                raise TypeError(
                    f"entry point returned {type(contribution).__name__}, "
                    "expected HarnessContribution"
                )
            error = _validate_community_contribution(
                contribution,
                entry_point_name=entry_point.name,
                existing=tuple(contributions),
            )
            if error is not None:
                raise ValueError(error)
            contributions.append(contribution)
        except Exception as exc:
            load_errors[entry_point.name] = str(exc)
            _logger.warning(
                "could not load harness plugin entry point %s (%s)",
                entry_point.name,
                exc,
                exc_info=True,
            )

    _state = HarnessPluginState(tuple(contributions), load_errors)
    return _state


def reset_plugin_state_for_tests() -> None:
    """Clear the cached plugin state."""
    global _state
    _state = None


_Value = TypeVar("_Value")


def _merge_dict(
    getter: Callable[[HarnessContribution], Mapping[str, _Value]],
) -> dict[str, _Value]:
    merged: dict[str, _Value] = {}
    for contribution in plugin_state().contributions:
        merged.update(getter(contribution))
    return merged


def _merge_set(
    getter: Callable[[HarnessContribution], Iterable[str]],
) -> frozenset[str]:
    merged: set[str] = set()
    for contribution in plugin_state().contributions:
        merged.update(getter(contribution))
    return frozenset(merged)


def valid_harnesses() -> frozenset[str]:
    """Return canonical harness ids accepted by installed contributions."""
    return _merge_set(lambda contribution: contribution.valid_harnesses)


def harness_aliases() -> dict[str, str]:
    """Return alias-to-canonical harness ids."""
    return _merge_dict(lambda contribution: contribution.aliases)


def accepted_harnesses() -> frozenset[str]:
    """Return canonical harness ids plus accepted aliases."""
    return valid_harnesses() | frozenset(harness_aliases())


def harness_modules() -> dict[str, str]:
    """Return runtime harness module mapping, aliases included."""
    modules = _merge_dict(lambda contribution: contribution.harness_modules)
    for alias, canonical in harness_aliases().items():
        module = modules.get(canonical)
        if module is not None:
            modules.setdefault(alias, module)
    return modules


def model_env_keys() -> dict[str, str]:
    """Return harness-to-model-env-var mapping."""
    return _merge_dict(lambda contribution: contribution.model_env_keys)


def spawn_env_builders() -> dict[str, str]:
    """Return harness-to-spawn-env-builder import paths."""
    return _merge_dict(lambda contribution: contribution.spawn_env_builders)


def install_specs() -> dict[str, HarnessInstallSpec]:
    """Return plugin-provided install specs."""
    return _merge_dict(lambda contribution: contribution.install_specs)


def harness_install_keys() -> dict[str, str]:
    """Return harness/alias to install-spec key mappings."""
    return _merge_dict(lambda contribution: contribution.harness_install_keys)


def missing_install_packages() -> dict[str, str]:
    """Return optional harness spellings to package names."""
    return _merge_dict(lambda contribution: contribution.missing_install_package)


def harness_labels() -> dict[str, str]:
    """Return labels for harness picker/catalog rows."""
    return _merge_dict(lambda contribution: contribution.harness_labels)


def harness_capabilities() -> dict[str, HarnessCapabilities]:
    """Return the declared capability record per harness id.

    Merged across contributions; sparse (a harness need not declare
    capabilities). This is the single source of truth for "what can this
    harness do?".
    """
    return _merge_dict(lambda contribution: contribution.capabilities)


def harness_catalog() -> list[dict[str, object]]:
    """Return stable JSON-serializable harness catalog rows.

    Each row carries ``id`` and ``label``; rows for harnesses with declared
    capabilities also carry a ``capabilities`` object (see
    :meth:`HarnessCapabilities.as_dict`). ``setup_steps`` lists the ordered
    requirements to get the harness ready on a host (install + auth), so the
    web UI can render a "set up this agent" checklist that mirrors
    ``omnigent setup``; the host reports each step's status in its readiness map.
    """
    labels = harness_labels()
    capabilities = harness_capabilities()
    # Lazy import: keep this registry importable without pulling in the
    # onboarding/config stack at module load.
    try:
        from omnigent.onboarding.harness_install import ui_setup_steps
    except Exception:
        _logger.debug("setup-step metadata unavailable", exc_info=True)
        ui_setup_steps = None  # type: ignore[assignment]
    rows: list[dict[str, object]] = []
    for harness in sorted(labels, key=lambda key: labels[key].lower()):
        if harness not in valid_harnesses():
            continue
        row: dict[str, object] = {"id": harness, "label": labels[harness]}
        capability = capabilities.get(harness)
        if capability is not None:
            row["capabilities"] = capability.as_dict()
        if ui_setup_steps is not None:
            row["setup_steps"] = [step.as_dict() for step in ui_setup_steps(harness)]
        rows.append(row)
    return rows


def harness_setup_steps_by_spelling() -> dict[str, list[dict[str, str | None]]]:
    """Map every harness spelling to its ordered UI setup steps.

    The web setup dialog looks steps up by the harness a *session* declares —
    which may be an installable id that is not a picker row, neither of which appears in
    :func:`harness_catalog`. Keying by spelling here lets the dialog resolve
    steps for whatever id it holds. Values mirror ``harness_catalog``'s
    ``setup_steps`` (same :func:`ui_setup_steps` source), so the two can't
    drift.

    :returns: ``{spelling: [step.as_dict(), ...]}`` for every accepted spelling;
        empty when the onboarding stack can't be imported (fail-open).
    """
    try:
        from omnigent.onboarding.harness_install import ui_installable_harnesses, ui_setup_steps
    except Exception:
        _logger.debug("setup-step metadata unavailable", exc_info=True)
        return {}
    # Cover the picker ids (catalog rows) plus every installable spelling
    # so a session's declared harness always resolves.
    spellings: set[str] = set(valid_harnesses())
    spellings.update(ui_installable_harnesses())
    return {
        spelling: [step.as_dict() for step in ui_setup_steps(spelling)] for spelling in spellings
    }


def load_object(import_path: str) -> object:
    """Load ``module:attribute`` or ``module.attribute``."""
    if ":" in import_path:
        module_name, attr = import_path.split(":", 1)
    else:
        module_name, attr = import_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)
