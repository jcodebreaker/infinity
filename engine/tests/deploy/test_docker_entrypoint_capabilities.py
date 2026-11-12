"""Guard: the boot-time capabilities log line survives a managed sandbox.

``build_app`` logs the flags ``/v1/info`` exposes so a missing ``sandbox:``
block shows up in pod logs without curling. That line reads the sandbox
deployment, and reading the wrong attribute there is fatal: it raises inside
``build_app``, before uvicorn binds, so the container crash-loops with the
server never becoming ready.

The expression is guarded by ``managed``, so it is only evaluated when a
sandbox block is configured AND launch-capable. A deployment with no managed
sandboxes never reaches it — which is why an ``AttributeError`` here can ship
green. These tests evaluate it directly, with and without a launch-capable
provider.
"""

from __future__ import annotations

import importlib
import logging

import pytest

from omnigent.server.hosts import HostDeployment, HostProviderConfig


@pytest.fixture
def entrypoint():
    """The Docker entrypoint module, imported from its real path."""
    return importlib.import_module("deployment.entrypoint")


def _deployment(*, launch_supported: bool) -> HostDeployment:
    """A single-provider deployment, as ``parse_host_section`` builds one."""
    return HostDeployment.single(
        HostProviderConfig(
            server_url="http://omnigent.example.svc.cluster.local",
            launcher_factory=lambda: None,  # never called: nothing launches here
            token_ttl_s=600,
            managed_launch_supported=launch_supported,
            provider="kubernetes",
        )
    )


def test_capabilities_logs_the_provider_for_a_launch_capable_deployment(
    entrypoint, caplog: pytest.LogCaptureFixture
) -> None:
    """The line must not raise, and must name the provider.

    Regression: ``HostDeployment`` wraps one config PER PROVIDER and
    exposes no ``provider`` of its own, so a bare ``sandbox_config.provider``
    raises ``AttributeError`` and kills the server at boot.
    """
    with caplog.at_level(logging.INFO, logger="omnigent-docker"):
        entrypoint.log_capabilities(_deployment(launch_supported=True), None, None)

    assert "managed_sandboxes=True" in caplog.text
    assert "provider=kubernetes" in caplog.text


def test_capabilities_reports_no_provider_when_launch_is_unsupported(
    entrypoint, caplog: pytest.LogCaptureFixture
) -> None:
    """A staged-only provider reports the flag false and no provider."""
    with caplog.at_level(logging.INFO, logger="omnigent-docker"):
        entrypoint.log_capabilities(_deployment(launch_supported=False), None, None)

    assert "managed_sandboxes=False" in caplog.text
    assert "provider=None" in caplog.text


def test_capabilities_handles_no_sandbox_block(
    entrypoint, caplog: pytest.LogCaptureFixture
) -> None:
    """No ``sandbox:`` block at all is the common deployment, and must be safe."""
    with caplog.at_level(logging.INFO, logger="omnigent-docker"):
        entrypoint.log_capabilities(None, None, None)

    assert "managed_sandboxes=False" in caplog.text
