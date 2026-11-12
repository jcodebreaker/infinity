"""Sandbox launchers: run Omnigent hosts in remote sandboxes.

The server's managed-host code launches hosts through these. Core Omnigent
contributes the built-in ``docker`` provider directly; third-party packages
can contribute providers through the ``omnigent.host_providers`` entry
point group.
"""

from __future__ import annotations

import click

from omnigent.hosts.launcher import (
    ExecModelHostLauncher,
    HostCapabilityError,
    HostGoneError,
    HostLauncher,
    RemoteCommandResult,
    RemoteProcess,
)
from omnigent.hosts.registry import (
    COMMUNITY_MODULE_PREFIX,
    HostProviderContribution,
    HostProviderMetadata,
    HostProviderPluginState,
    HostRegistryError,
    available_providers,
    get_provider_metadata,
    instantiate,
    plugin_state,
    reset_plugin_state_for_tests,
)
from omnigent.hosts.types import (
    GitCloneOptions,
    HostCapabilities,
    HostCommandError,
    HostConfigError,
    HostContext,
    HostError,
    HostInfo,
    HostSpec,
    RepoWorkspace,
)

__all__ = [
    "COMMUNITY_MODULE_PREFIX",
    "ExecModelHostLauncher",
    "GitCloneOptions",
    "HostCapabilities",
    "HostCapabilityError",
    "HostCommandError",
    "HostConfigError",
    "HostContext",
    "HostError",
    "HostGoneError",
    "HostInfo",
    "HostLauncher",
    "HostProviderContribution",
    "HostProviderMetadata",
    "HostProviderPluginState",
    "HostRegistryError",
    "HostSpec",
    "RemoteCommandResult",
    "RemoteProcess",
    "RepoWorkspace",
    "available_providers",
    "get_launcher",
    "get_provider_metadata",
    "plugin_state",
    "reset_plugin_state_for_tests",
]


def get_launcher(provider: str) -> HostLauncher:
    """
    Resolve a provider name to a launcher instance.

    :param provider: Provider name, e.g. ``"docker"``.
    :returns: A fresh launcher for the provider.
    :raises click.ClickException: If the provider is unknown or its
        launcher module is not present in this build.
    """
    if provider not in plugin_state():
        offered = ", ".join(available_providers()) or "(none in this build)"
        raise click.ClickException(
            f"Unknown or unavailable host provider '{provider}'. Available: {offered}."
        )
    try:
        return instantiate(provider)
    except HostRegistryError as exc:
        raise click.ClickException(str(exc)) from exc
