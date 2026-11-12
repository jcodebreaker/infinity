"""Tests for ``omnigent.superchat.memory.memory_profile_for`` (S6).

The one function other slices call for a user's Memory Profile, already
wrapped in the per-turn delimiter block.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from omnigent.superchat.memory import memory_profile_for


class _FakeMemoryService:
    def __init__(self, profiles: dict[str, str | None]) -> None:
        self._profiles = profiles

    def profile(self, user_id: str) -> str | None:
        return self._profiles.get(user_id)


class _ExplodingMemoryService:
    """Simulates a profile render failure (e.g. a DB or index error)."""

    def profile(self, user_id: str) -> str | None:
        raise RuntimeError("boom")


def test_returns_none_when_no_service_is_configured(monkeypatch: Any) -> None:
    monkeypatch.setattr("omnigent.runtime.get_memory_service", lambda: None)
    assert memory_profile_for("alice") is None


def test_returns_none_when_the_profile_is_empty(monkeypatch: Any) -> None:
    service = _FakeMemoryService({"alice": None})
    monkeypatch.setattr("omnigent.runtime.get_memory_service", lambda: service)
    assert memory_profile_for("alice") is None


def test_wraps_a_non_empty_profile_in_the_delimiter_block(monkeypatch: Any) -> None:
    service = _FakeMemoryService({"alice": "Preferences:\n- Prefers figures in CAD"})
    monkeypatch.setattr("omnigent.runtime.get_memory_service", lambda: service)
    block = memory_profile_for("alice")
    assert block is not None
    assert block.startswith("[Standing memory about the user")
    assert block.endswith("[End of standing memory]")
    assert "Prefers figures in CAD" in block


def test_prefers_an_explicitly_passed_service_over_the_global(monkeypatch: Any) -> None:
    """A caller holding its own instance (e.g. a route closure) must never
    read a stale or differently-configured runtime global."""
    global_service = _FakeMemoryService({"alice": "Preferences:\n- From the global"})
    explicit_service = _FakeMemoryService({"alice": "Preferences:\n- From the explicit service"})
    monkeypatch.setattr("omnigent.runtime.get_memory_service", lambda: global_service)

    block = memory_profile_for("alice", service=explicit_service)
    assert block is not None
    assert "From the explicit service" in block
    assert "From the global" not in block


def test_is_isolated_per_user(monkeypatch: Any) -> None:
    service = _FakeMemoryService({"alice": "Preferences:\n- Alice's preference", "bob": None})
    monkeypatch.setattr("omnigent.runtime.get_memory_service", lambda: service)
    assert memory_profile_for("bob") is None
    block = memory_profile_for("alice")
    assert block is not None
    assert "Alice's preference" in block


def test_render_failure_is_logged_and_the_turn_proceeds_without_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A profile render failure must never raise out to the caller (the
    turn proceeds with no Memory Profile) and must be logged once, at
    warning level, with no profile content in the message."""
    with caplog.at_level(logging.WARNING, logger="omnigent.superchat.memory.profile"):
        block = memory_profile_for("alice", service=_ExplodingMemoryService())

    assert block is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert "boom" not in warnings[0].getMessage()
