"""Tests for :mod:`omnigent.server.routing_settings`.

Covers :func:`_build_external_routing_client` (the external
``routes:select`` provider), including config validation that degrades to
``None`` (routing disabled) rather than raising.
"""

from __future__ import annotations

from typing import Any

import click
import pytest

from omnigent.server.routing_settings import _build_external_routing_client
from omnigent.server.smart_routing import ExternalRoutingClient


def test_external_builds_client() -> None:
    cfg = {
        "provider": "external",
        "base_url": "https://host/ai-gateway/routing/v1",
        "router_name": "task_v0",
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    assert client._url == "https://host/ai-gateway/routing/v1/routes:select"
    assert client._router_name == "task_v0"
    assert client._auth is None  # no api_key -> unauthenticated
    # No prefix configured -> the module's shared catalog-prefix list, so the
    # client and the server-side seam can't disagree about a catalog id.
    from omnigent.server.smart_routing import MODEL_ID_PREFIXES

    assert client._model_prefixes == list(MODEL_ID_PREFIXES)


def test_external_threads_model_prefix_scalar() -> None:
    """A single ``model_prefix`` string is wrapped into a one-element list."""
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "model_prefix": "databricks-",
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    assert client._model_prefixes == ["databricks-"]


def test_external_threads_model_prefix_list() -> None:
    """A list of prefixes threads through verbatim (blanks dropped)."""
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "model_prefix": ["databricks-", "system.ai.", ""],
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    assert client._model_prefixes == ["databricks-", "system.ai."]


def test_external_ignores_a_profile_key() -> None:
    """Only ``api_key`` authenticates; a ``profile`` is not a supported auth mode."""
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "profile": "staging",
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    assert client._auth is None


def test_external_api_key_expands_env(monkeypatch: Any) -> None:
    """api_key is provider-agnostic and ${ENV}-expanded into a bearer header."""
    import httpx

    monkeypatch.setenv("OMNIGENT_TEST_ROUTING_KEY", "sekret")
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "api_key": "${OMNIGENT_TEST_ROUTING_KEY}",
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    # The bearer auth carries the expanded token.
    request = httpx.Request("POST", "https://host/v1/routes:select")
    flow = client._auth.auth_flow(request)
    assert next(flow).headers["Authorization"] == "Bearer sekret"


def test_external_missing_required_fields_disables() -> None:
    """base_url and router_name are both required; missing either disables."""
    assert _build_external_routing_client({"provider": "external", "router_name": "x"}) is None
    assert (
        _build_external_routing_client({"provider": "external", "base_url": "https://h/v1"})
        is None
    )


def test_external_rejects_non_string_config_value() -> None:
    with pytest.raises(click.ClickException, match=r"routing\.base_url must be a string"):
        _build_external_routing_client(
            {
                "provider": "external",
                "base_url": 123,
                "router_name": "task_v0",
            }
        )
