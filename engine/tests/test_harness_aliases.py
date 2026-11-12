"""Tests for omnigent.harness_aliases."""

from __future__ import annotations

import pytest

from omnigent.harness_aliases import canonicalize_harness, is_claude_sdk_harness_name


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("claude", "claude-sdk"),
        # Canonical names pass through unchanged.
        ("claude-sdk", "claude-sdk"),
        ("pi", "pi"),
        # Unknown names return unchanged so callers keep their own errors.
        ("bogus", "bogus"),
        (None, None),
    ],
)
def test_canonicalize_harness(alias: str | None, canonical: str | None) -> None:
    """Alias spellings map to canonical ids; everything else passes through."""
    assert canonicalize_harness(alias) == canonical


@pytest.mark.parametrize(
    "harness,expected",
    [
        ("claude-sdk", True),
        ("claude", True),
        ("pi", False),
        (None, False),
    ],
)
def test_is_claude_sdk_harness_name(harness: str | None, expected: bool) -> None:
    """The Claude SDK check accepts the canonical id and its ``claude`` alias."""
    assert is_claude_sdk_harness_name(harness) is expected
