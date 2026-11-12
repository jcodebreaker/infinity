"""Shared harness-name alias helpers.

Keep user-facing shorthand spellings at the edges while the rest of
Omnigent continues to use canonical harness identifiers internally.
"""

from __future__ import annotations

from omnigent.harness_plugins import harness_aliases

HARNESS_ALIASES: dict[str, str] = harness_aliases()


def canonicalize_harness(harness: str | None) -> str | None:
    """Return the canonical harness identifier for *harness*.

    Unknown names are returned unchanged so callers can still produce
    their normal validation error messages.
    """
    if harness is None:
        return None
    return HARNESS_ALIASES.get(harness, harness)


def is_claude_sdk_harness_name(harness: str | None) -> bool:
    """Return ``True`` for the canonical Claude SDK harness and aliases."""
    return canonicalize_harness(harness) == "claude-sdk"
