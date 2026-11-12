"""The Conversation may update only the topic the person picked up, for 3 hours (ADR 0035)."""

from __future__ import annotations

from omnigent.superchat.topics.note import (
    ACTIVE_SECONDS,
    active_key,
    active_value,
    conversation_refusal,
)

PICKED = 1_800_000_000


def test_the_picked_topic_stays_active_for_three_hours_only() -> None:
    value = active_value("polaris-deck", PICKED)
    assert active_key(value, PICKED + 60) == "polaris-deck"
    assert active_key(value, PICKED + ACTIVE_SECONDS) == "polaris-deck"
    assert active_key(value, PICKED + ACTIVE_SECONDS + 1) == ""
    for unset in ("", None, "polaris-deck", "polaris-deck\tsoon", "\t1800000000"):
        assert active_key(unset, PICKED) == ""


def test_the_conversation_writes_only_its_active_topic() -> None:
    assert conversation_refusal(["polaris-deck"], "polaris-deck") == ""
    assert "polaris-deck" in conversation_refusal(["polaris-deck", "polaris"], "polaris-deck")
    assert conversation_refusal([""], "polaris-deck")
    assert conversation_refusal(["polaris-deck"], "")
