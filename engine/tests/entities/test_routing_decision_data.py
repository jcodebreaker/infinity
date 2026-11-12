"""
Tests for the routing-identity fields on ``RoutingDecisionData``.

The intelligent-routing MVP adds harness / scope / decision identity to
the routing-decision transcript item. Every field is defaulted so rows
persisted before they existed still deserialize.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.entities.conversation import RoutingDecisionData, parse_item_data

_LEGACY_ROW = {
    "model": "databricks-claude-opus-4-8",
    "applied": True,
    "rationale": "Multi-file refactor needs deep reasoning.",
}


def test_legacy_row_deserializes_with_defaults() -> None:
    data = parse_item_data("routing_decision", dict(_LEGACY_ROW))
    assert isinstance(data, RoutingDecisionData)
    assert data.model == "databricks-claude-opus-4-8"
    assert data.harness is None
    assert data.scope == "turn"
    assert data.decision_id is None
    assert data.raw_model is None
    assert data.attempted_override is None
    assert data.router_source is None


@pytest.mark.parametrize("scope", ["session", "turn", "child_session", "native_subagent"])
def test_every_scope_value_validates(scope: str) -> None:
    data = RoutingDecisionData(
        model="databricks-claude-sonnet-5",
        applied=True,
        rationale="ok",
        scope=scope,  # type: ignore[arg-type]
    )
    assert data.scope == scope


def test_unknown_scope_rejected() -> None:
    with pytest.raises(ValidationError):
        RoutingDecisionData(
            model="databricks-claude-sonnet-5",
            applied=True,
            rationale="ok",
            scope="galaxy",  # type: ignore[arg-type]
        )


# ── router_source ───────────────────────────────────────────────────────────
#
# Which router produced the decision. A plain ``str``, not a ``Literal``: a
# source added later must round-trip through stored rows and the wire rather
# than failing validation on the way back in.


@pytest.mark.parametrize("source", ["databricks-aigw", "oss-llm", "some-future-router"])
def test_any_router_source_round_trips(source: str) -> None:
    original = RoutingDecisionData(
        model="databricks-claude-sonnet-5",
        applied=True,
        rationale="ok",
        router_source=source,
    )
    dumped = original.model_dump()
    assert dumped["router_source"] == source
    assert RoutingDecisionData(**dumped) == original


def test_an_omitted_router_source_parses_as_none() -> None:
    data = parse_item_data("routing_decision", dict(_LEGACY_ROW))
    assert isinstance(data, RoutingDecisionData)
    assert data.router_source is None
    # The field still rides the wire as an explicit null rather than vanishing.
    assert data.model_dump()["router_source"] is None
