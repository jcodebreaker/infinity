"""Readers for a host's reported per-harness gateway-backed map."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.gateway_inference import gateway_inference_state, not_gateway_backed


@pytest.mark.parametrize(
    ("gateway", "harness", "expected"),
    [
        ({"claude-sdk": False}, "claude-sdk", False),
        ({"claude-sdk": True}, "claude-sdk", True),
        # An alias resolves through its canonical id.
        ({"claude-sdk": True}, "claude", True),
        ({"pi": False}, "pi", False),
        # Unknown: an absent entry or an absent map reads as "could not tell".
        ({"claude-sdk": True}, "pi", None),
        (None, "pi", None),
        ({}, "pi", None),
        ({"pi": False}, "some-unknown-harness", None),
    ],
)
def test_gateway_inference_state_reads_a_reported_map(
    gateway: dict[str, Any] | None,
    harness: str,
    expected: bool | None,
) -> None:
    assert gateway_inference_state(gateway, harness) is expected


def test_not_gateway_backed_names_only_the_explicit_false_arms() -> None:
    arms = ("claude-sdk", "pi")
    assert not_gateway_backed({"claude-sdk": True, "pi": False}, arms) == ["pi"]
    assert not_gateway_backed({"claude-sdk": False, "pi": False}, arms) == list(arms)
    assert not_gateway_backed({"claude-sdk": True, "pi": True}, arms) == []
    # Unknown keeps every option: an older host must not silently lose routing.
    assert not_gateway_backed(None, arms) == []
    assert not_gateway_backed({"claude-sdk": True}, arms) == []
