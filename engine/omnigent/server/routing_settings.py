"""Parse the engine config's ``routing:`` block and build its external router.

The engine entry point (``deployment/entrypoint.py``) reads ``routing.*`` here
once and hands the result to ``RuntimeCaps``; every consumer reads the parsed
:class:`~omnigent.server.smart_routing.RoutingSettings` from there.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import click


def _parse_model_prefixes(
    raw: object,
) -> list[str] | None:
    """Normalize the ``model_prefix`` config into a list of prefixes.

    Accepts a single string (``"vendor-"``) or a list
    (``["vendor-", "system.ai."]``); blanks are dropped.

    :returns: The configured prefixes — an explicit empty list is honoured as
        "this catalog carries no prefix" — or ``None`` when the key is absent or
        malformed, leaving :data:`MODEL_ID_PREFIXES` in place.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    return [p.strip() for p in raw if isinstance(p, str) and p.strip()]


def _routing_config_text(routing_cfg: Mapping[str, object], key: str) -> str:
    value = routing_cfg.get(key)
    if isinstance(value, str):
        return value.strip()
    if not value:
        return ""
    raise click.ClickException(f"routing.{key} must be a string")


def parse_routing_settings(
    routing_cfg: Any,  # type: ignore[explicit-any]  # parsed YAML block
) -> Any:  # type: ignore[explicit-any]  # RoutingSettings
    """Parse the ``routing:`` block into the shared ``RoutingSettings``.

    This is the only place ``routing.*`` config is read; every consumer
    (the routing clients, the subagent router) reads the dataclass off
    ``RuntimeCaps`` instead.

    :param routing_cfg: The parsed ``routing:`` mapping, or ``None``.
    :returns: A :class:`~omnigent.server.smart_routing.RoutingSettings`;
        all-defaults when the block is absent or malformed.
    """
    from omnigent.server.smart_routing import (
        DEFAULT_ROUTER_NAME,
        MODEL_ID_PREFIXES,
        RoutingSettings,
        parse_routing_tables,
    )

    if not isinstance(routing_cfg, dict):
        return RoutingSettings()
    router_name = (routing_cfg.get("router_name") or "").strip() or DEFAULT_ROUTER_NAME
    selection_model = (routing_cfg.get("selection_model") or "").strip() or None
    prefixes = _parse_model_prefixes(routing_cfg.get("model_prefix"))
    return RoutingSettings(
        router_name=router_name,
        selection_model=selection_model,
        # Only an absent key falls back: ``model_prefix: []`` means bare ids.
        model_prefixes=MODEL_ID_PREFIXES if prefixes is None else tuple(prefixes),
        # The arm menu / alias / effort tables a deployment fronting a different
        # catalog overrides; absent keys keep the built-in defaults.
        **parse_routing_tables(routing_cfg),
    )


def _build_external_routing_client(
    routing_cfg: Any,  # type: ignore[explicit-any]  # parsed YAML block
    settings: Any = None,  # type: ignore[explicit-any]  # RoutingSettings | None
) -> Any | None:  # type: ignore[explicit-any]  # ExternalRoutingClient | None
    """Build an :class:`ExternalRoutingClient` from the ``routing:`` config.

    Requires ``base_url`` + ``router_name``. Auth is an explicit ``api_key``
    (``${ENV}`` expanded) sent as a static bearer, else unauthenticated.
    Optional ``model_prefix`` (a single prefix or a list of prefixes) is
    stripped from catalog model ids sent to the router (and restored on its
    answer) — e.g. when catalog ids carry a prefix the router does not key on.

    :param routing_cfg: The parsed ``routing:`` mapping (a dict with
        ``provider == "external"``, per the caller).
    :param settings: The parsed routing settings, supplying the extraction
        model, scenario menus, and model prefixes. ``None`` parses them from
        *routing_cfg*.
    :returns: A configured client, or ``None`` when required config is
        missing (a warning is logged; routing stays off rather than raising).
    """
    if settings is None:
        settings = parse_routing_settings(routing_cfg)
    base_url = _routing_config_text(routing_cfg, "base_url")
    router_name = _routing_config_text(routing_cfg, "router_name")
    api_key = _routing_config_text(routing_cfg, "api_key")

    if not base_url or not router_name:
        click.echo(
            "routing.provider=external requires base_url and router_name; skipping",
            err=True,
        )
        return None

    from omnigent.server.smart_routing import _bearer_auth

    # An explicit api_key is sent as a static bearer; otherwise the request
    # goes out unauthenticated.
    auth = None
    if api_key:
        from omnigent.spec import expand_env_vars

        auth = _bearer_auth(expand_env_vars({"api_key": api_key})["api_key"])

    from omnigent.server.smart_routing import ExternalRoutingClient

    return ExternalRoutingClient(
        base_url=base_url,
        router_name=router_name,
        auth=auth,
        model_prefixes=list(settings.model_prefixes),
        selection_model=settings.selection_model,
        menus=settings.menus,
        servable_aliases=settings.servable_aliases,
    )
