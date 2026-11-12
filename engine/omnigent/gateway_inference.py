"""Readers for a host's reported per-harness AI-Gateway-backed map.

Smart Routing's apply layer can only rewrite a launch's model when the launch
resolves through an AI Gateway that serves the routable model catalog. Hosts
may report that per harness in their ``gateway_inference`` registration map;
these helpers read it. An absent map or entry is *unknown*, which never gates
routing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def gateway_inference_state(
    gateway: Mapping[str, object] | None,
    harness: str,
) -> bool | None:
    """Read *harness*'s gateway-backed flag out of a reported map.

    :param gateway: A host's ``gateway_inference`` map, or ``None``.
    :param harness: Harness id in any spelling, e.g. ``"claude"``.
    :returns: The reported flag, or ``None`` when the map says nothing about
        this harness — an older host, a family whose check could not run, or a
        host that has not registered yet. Unknown is not "unavailable".
    """
    if not gateway:
        return None
    from omnigent.harness_aliases import canonicalize_harness

    for key in (canonicalize_harness(harness) or harness, harness):
        value = gateway.get(key)
        if isinstance(value, bool):
            return value
    return None


def not_gateway_backed(
    gateway: Mapping[str, object] | None,
    harnesses: Iterable[str],
) -> list[str]:
    """Which of *harnesses* the map explicitly reports as not gateway-backed.

    Smart Routing's apply layer rewrites the launch model through the AI
    Gateway, so these are the harnesses a routed pick could not reach. Only an
    explicit ``False`` counts: unknown keeps every option.

    :param gateway: A host's ``gateway_inference`` map, or ``None``.
    :param harnesses: Harness ids to check, e.g. ``("claude-sdk",)``.
    :returns: The not-backed ids, in the order given.
    """
    return [harness for harness in harnesses if gateway_inference_state(gateway, harness) is False]
