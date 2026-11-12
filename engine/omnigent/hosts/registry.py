"""Host provider registry.

Core Omnigent contributes the built-in ``docker`` provider directly.
Third-party packages contribute additional providers through the
``omnigent.host_providers`` entry point group.

The registry mirrors the design of ``omnigent.harness_plugins``:
contributions are merged into a single plugin state, validation keeps
community provider code in the ``omnigent.community.host`` namespace, and
broken plugins are recorded but never break core startup.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from omnigent.hosts.launcher import HostLauncher

logger = logging.getLogger(__name__)

COMMUNITY_ENTRY_POINT_GROUP = "omnigent.host_providers"
COMMUNITY_MODULE_PREFIX = "omnigent.community.host."


class HostRegistryError(Exception):
    """A provider registry operation failed."""


class _ConfiguredLauncherFactory(Protocol):
    def __call__(self, *, config: object) -> HostLauncher:
        pass


@dataclass(frozen=True)
class HostProviderMetadata:
    """Static metadata for one host provider.

    :param name: Provider short name, e.g. ``"docker"``.
    :param launcher_class: Fully-qualified launcher class as
        ``"module.path:ClassName"`` or ``"module.path.ClassName"``.
    :param config_model: Optional Pydantic model that validates the
        provider-specific ``host.<name>`` config block.
    :param managed_token_ttl_s: Optional default managed launch-token
        lifetime in seconds.
    :param workspace_survives_recreate: Whether a sandbox recreated for the
        same session finds the files the old one left (e.g. a home directory
        mounted from durable storage). When ``True`` a recreate is not a
        workspace reset, so the session gets no "files are gone" notice.
    """

    name: str
    launcher_class: str
    config_model: type[object] | None = None
    managed_token_ttl_s: int | None = None
    workspace_survives_recreate: bool = False


@dataclass(frozen=True)
class HostProviderContribution:
    """One package's contribution to the host provider registry.

    :param name: Package / contributor name, e.g. ``"omnigent-acme"``. Used
        only for error messages.
    :param providers: Mapping of provider short name to metadata.
    """

    name: str
    providers: dict[str, HostProviderMetadata] = field(default_factory=dict)


@dataclass(frozen=True)
class HostProviderPluginState:
    """Merged provider registry plus non-fatal plugin load errors."""

    providers: dict[str, HostProviderMetadata]
    load_errors: dict[str, str]

    def __contains__(self, name: str) -> bool:
        return name in self.providers

    def get(self, name: str) -> HostProviderMetadata | None:
        return self.providers.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self.providers)


_state: HostProviderPluginState | None = None


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    """Return community host-provider entrypoints, tolerating old stdlib."""
    discovered = importlib.metadata.entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=COMMUNITY_ENTRY_POINT_GROUP))
    legacy = cast(Mapping[str, Iterable[importlib.metadata.EntryPoint]], discovered)
    return tuple(legacy.get(COMMUNITY_ENTRY_POINT_GROUP, ()))


def _load_object(import_path: str) -> Any:
    """Load ``module:attribute`` or ``module.attribute``."""
    if ":" in import_path:
        module_name, attr = import_path.split(":", 1)
    else:
        module_name, attr = import_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _launcher_module(import_path: str) -> str:
    """Return the module part of a launcher class import path."""
    if ":" in import_path:
        return import_path.split(":", 1)[0]
    return import_path.rsplit(".", 1)[0]


def _config_model_module(config_model: type[object]) -> str | None:
    """Return the module of a config model class, or ``None``."""
    return getattr(config_model, "__module__", None)


def _builtin_contribution() -> HostProviderContribution:
    """The built-in host-provider contribution from core Omnigent."""
    return HostProviderContribution(
        name="omnigent",
        providers={
            "docker": HostProviderMetadata(
                name="docker",
                launcher_class="omnigent.hosts.docker:DockerHostLauncher",
                managed_token_ttl_s=7 * 24 * 3600,
                # The host's home is a per-person Docker volume; it outlives the container.
                workspace_survives_recreate=True,
            ),
        },
    )


def _provider_module_available(import_path: str) -> bool:
    """Return whether the module holding a launcher class is importable."""
    module_name = _launcher_module(import_path)
    return importlib.util.find_spec(module_name) is not None


def _validate_community_contribution(
    contribution: HostProviderContribution,
    *,
    entry_point_name: str,
    existing: dict[str, HostProviderMetadata],
) -> str | None:
    """Validate a community contribution. Returns an error string or ``None``."""
    if not contribution.name:
        return "host provider plugin must set name"

    for name, meta in contribution.providers.items():
        if not meta.name:
            return f"provider {name!r} must set name"
        if name != meta.name:
            return f"provider key {name!r} does not match metadata name {meta.name!r}"
        if not meta.launcher_class:
            return f"provider {name!r} must set launcher_class"
        if name in existing:
            return (
                f"host provider plugin {entry_point_name!r} attempts to "
                f"override existing provider {name!r}"
            )

        module = _launcher_module(meta.launcher_class)
        if not module.startswith(COMMUNITY_MODULE_PREFIX):
            return (
                f"host provider plugin {entry_point_name!r} provider "
                f"{name!r} uses module {module!r}; expected "
                f"{COMMUNITY_MODULE_PREFIX}*"
            )

        if meta.config_model is not None:
            cfg_module = _config_model_module(meta.config_model)
            if cfg_module is None or not cfg_module.startswith(COMMUNITY_MODULE_PREFIX):
                return (
                    f"host provider plugin {entry_point_name!r} config model "
                    f"for provider {name!r} must live under "
                    f"{COMMUNITY_MODULE_PREFIX}*"
                )
    return None


def plugin_state() -> HostProviderPluginState:
    """Return the merged built-in + community host provider registry."""
    global _state
    if _state is not None:
        return _state

    built_in = _builtin_contribution().providers
    providers: dict[str, HostProviderMetadata] = {}
    load_errors: dict[str, str] = {}

    for name, meta in built_in.items():
        if _provider_module_available(meta.launcher_class):
            providers[name] = meta

    for entry_point in _entry_points():
        try:
            loaded = entry_point.load()
            contribution = loaded() if callable(loaded) else loaded
            if not isinstance(contribution, HostProviderContribution):
                raise TypeError(
                    f"entry point returned {type(contribution).__name__}, "
                    "expected HostProviderContribution"
                )
            error = _validate_community_contribution(
                contribution,
                entry_point_name=entry_point.name,
                existing=providers,
            )
            if error is not None:
                raise ValueError(error)
            providers.update(contribution.providers)
        except Exception as exc:
            load_errors[entry_point.name] = str(exc)
            logger.warning(
                "could not load host provider entry point %s (%s)",
                entry_point.name,
                exc,
                exc_info=True,
            )

    _state = HostProviderPluginState(providers=providers, load_errors=load_errors)
    return _state


def reset_plugin_state_for_tests() -> None:
    """Clear the cached plugin state."""
    global _state
    _state = None


def available_providers() -> tuple[str, ...]:
    """All host providers whose launcher modules are available."""
    return plugin_state().names()


def get_provider_metadata(name: str) -> HostProviderMetadata | None:
    """Return metadata for a provider, or ``None`` if it is not registered."""
    return plugin_state().get(name)


def instantiate(
    name: str,
    *,
    config: Mapping[str, object] | None = None,
) -> HostLauncher:
    """Import and instantiate a registered provider's launcher class.

    :param name: Registered provider name.
    :param config: The provider's ``host.<name>`` block, or ``None``.
        Only used when the provider declares a
        :attr:`HostProviderMetadata.config_model`: the block is
        validated through that model and the result is passed to the
        launcher constructor as ``config=``. Providers that declare no
        model — which is every built-in — ignore this entirely, so the
        argument changes nothing for them.
    :returns: A fresh launcher instance.
    :raises HostRegistryError: If the provider is unknown, its class
        cannot be imported/instantiated, or *config* fails validation
        against the declared model.
    """
    meta = get_provider_metadata(name)
    if meta is None:
        raise HostRegistryError(f"unknown host provider '{name}'")
    try:
        launcher_cls = _load_object(meta.launcher_class)
    except Exception as exc:
        raise HostRegistryError(
            f"could not load host provider '{name}' from {meta.launcher_class!r}: {exc}"
        ) from exc
    if not isinstance(launcher_cls, type) or not issubclass(launcher_cls, HostLauncher):
        raise HostRegistryError(
            f"host provider '{name}' resolved to {launcher_cls!r}, "
            f"which is not a HostLauncher subclass"
        )
    if meta.config_model is not None:
        # The docstring on `config_model` already describes it as validating
        # "the provider-specific `host.<name>` config block"; this is the
        # call that makes that true. Kept inside the `is not None` branch so a
        # provider that declares no model keeps a zero-argument constructor.
        try:
            validated = meta.config_model(**dict(config or {}))
        except Exception as exc:
            raise HostRegistryError(
                f"invalid 'host.{name}' config for provider '{name}': {exc}"
            ) from exc
        return cast(_ConfiguredLauncherFactory, launcher_cls)(config=validated)
    return launcher_cls()
