"""A Super Chat turn's prefix never holds the model call past its budget (ADR 0026)."""

from __future__ import annotations

import asyncio
import time

import pytest

from omnigent.superchat import prompt_prefix
from omnigent.superchat.local_time import LOCAL_TIME_OPEN

_BUDGET = 0.1


@pytest.fixture(autouse=True)
def _quick_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every read answers at once unless a test slows it; the budget is short."""

    async def profile(_server: object, _conv: str) -> str | None:
        return None

    async def features(_server: object, _conv: str) -> list[str]:
        return []

    async def timezone(_server: object, **_kwargs: object) -> str | None:
        return "America/Toronto"

    monkeypatch.setattr(prompt_prefix, "PREFIX_BUDGET_SECONDS", _BUDGET)
    monkeypatch.setattr(prompt_prefix, "fetch_memory_profile", profile)
    monkeypatch.setattr(prompt_prefix, "_safe_projects_block", lambda _workspace: None)
    monkeypatch.setattr(prompt_prefix, "_feature_prefix_blocks", features)
    monkeypatch.setattr(prompt_prefix, "fetch_timezone", timezone)
    prompt_prefix._late_feature_blocks.clear()
    prompt_prefix._known_timezone.clear()


async def test_a_slow_read_is_left_out_and_the_prefix_keeps_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_profile(_server: object, _conv: str) -> str:
        await asyncio.sleep(5)
        return "PROFILE"

    monkeypatch.setattr(prompt_prefix, "fetch_memory_profile", slow_profile)
    sent_now: dict[str, str] = {}
    started = time.monotonic()
    blocks = await prompt_prefix.turn_prefix_blocks(None, "conv", sent_now=sent_now)
    assert time.monotonic() - started < 1.0
    assert "PROFILE" not in blocks
    assert blocks[-1].startswith(LOCAL_TIME_OPEN)
    # Not sent, so not remembered: the next turn carries the profile in full.
    assert "profile" not in sent_now


async def test_feature_notes_read_late_go_with_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a note consumes it, so a late one is delivered next, unless superseded."""
    calls: list[int] = []

    async def features(_server: object, _conv: str) -> list[str]:
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(3 * _BUDGET)
            return ["Edited report.html by hand.", "[Artifacts in this chat]\n- deck v1"]
        return ["[Artifacts in this chat]\n- deck v2"]

    monkeypatch.setattr(prompt_prefix, "_feature_prefix_blocks", features)
    first = await prompt_prefix.turn_prefix_blocks(None, "conv")
    assert not any("Edited" in block for block in first)

    await asyncio.sleep(4 * _BUDGET)
    second = await prompt_prefix.turn_prefix_blocks(None, "conv")
    assert second[:-1] == ["Edited report.html by hand.", "[Artifacts in this chat]\n- deck v2"]


async def test_a_late_timezone_read_keeps_the_zone_last_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await prompt_prefix.turn_prefix_blocks(None, "conv")
    assert "America/Toronto" in first[-1]

    async def hung(_server: object, **_kwargs: object) -> str | None:
        await asyncio.sleep(5)
        return None

    monkeypatch.setattr(prompt_prefix, "fetch_timezone", hung)
    second = await prompt_prefix.turn_prefix_blocks(None, "conv")
    assert "America/Toronto" in second[-1]


async def test_a_message_attaching_a_file_waits_for_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person's own material has the longer bound: the file's text is never dropped."""

    async def file_text(
        _server: object, _conv: str, _texts: object, helper: bool = False
    ) -> list[str]:
        await asyncio.sleep(3 * _BUDGET)
        return ["[Attached: notes.pdf]"]

    monkeypatch.setattr(prompt_prefix, "_message_prefix_blocks", file_text)
    monkeypatch.setattr(prompt_prefix, "message_needs_own_turn", lambda _texts: True)
    blocks = await prompt_prefix.turn_prefix_blocks(None, "conv", message_texts=["see file"])
    assert blocks[0] == "[Attached: notes.pdf]"
