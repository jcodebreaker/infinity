"""Tests for the ``render_card`` reply-card tool."""

import json

import pytest

from omnigent.runner.tool_dispatch import should_dispatch_locally
from omnigent.spec.types import AgentSpec
from omnigent.superchat.cards.tools import CARD_DATA_SCHEMAS, RenderCardTool
from omnigent.tools.base import ToolContext
from omnigent.tools.manager import ToolManager

_CTX = ToolContext(task_id="", conversation_id="", agent_id="")

_VALID: dict[str, dict] = {
    "sources": {"items": [{"title": "A", "url": "https://a.test", "snippet": "s"}]},
    "compare": {"columns": ["X", "Y"], "rows": [{"label": "Row", "cells": ["1", "2"]}]},
    "quote": {"symbol": "ACME", "price": 12.5, "changePct": -1.2},
    "person": {"name": "Ada"},
    "file": {"name": "report.pdf", "size": 1024},
    "progress": {"label": "Import", "value": 40, "status": "running"},
}


def _call(**args: object) -> dict:
    return json.loads(RenderCardTool().invoke(json.dumps(args), _CTX))


@pytest.mark.parametrize("kind", sorted(set(_VALID) - {"progress"}))
def test_terminal_card_tells_the_model_to_end_its_turn(kind: str) -> None:
    # A finished answer card: no trailing line repeating it.
    out = _call(card=kind, data=_VALID[kind], fallback="s")
    assert "end your turn" in out["note"]


def test_living_card_does_not_end_the_turn() -> None:
    # A mid-work progress card is re-rendered under the same id; the Assistant keeps going.
    out = _call(card="progress", id="c1", data=_VALID["progress"], fallback="s")
    assert "end your turn" not in out["note"]


def test_chart_is_not_a_card_kind_any_more() -> None:
    # Charts are pages superchat-dashboard builds from files, never numbers the model types.
    # The plan card went with the plan machinery (ADR 0029).
    assert "chart" not in CARD_DATA_SCHEMAS and "plan" not in CARD_DATA_SCHEMAS
    out = _call(
        card="chart",
        data={"kind": "line", "x": ["a"], "series": [{"name": "s", "values": [1]}]},
        fallback="s",
    )
    assert "card must be one of" in out["error"]
    assert (
        "chart"
        not in RenderCardTool().get_schema()["function"]["parameters"]["properties"]["card"][
            "enum"
        ]
    )
    assert "superchat-dashboard" in RenderCardTool.description()


def test_every_kind_has_a_valid_fixture() -> None:
    assert set(_VALID) == set(CARD_DATA_SCHEMAS)


@pytest.mark.parametrize("kind", sorted(_VALID))
def test_valid_card_returns_typed_payload(kind: str) -> None:
    out = _call(card=kind, id="c1", title="T", data=_VALID[kind], fallback=" Summary ")
    out.pop("note")
    assert out == {
        "type": "card",
        "card": kind,
        "id": "c1",
        "title": "T",
        "data": _VALID[kind],
        "fallback": "Summary",
    }


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"card": "nope", "data": {}, "fallback": "x"}, "card must be one of"),
        ({"card": "quote", "data": {"symbol": "A", "price": 1}}, "fallback"),
        ({"card": "quote", "data": {"symbol": "A"}, "fallback": "x"}, "data.price is required"),
        (
            {"card": "quote", "data": {"symbol": "A", "price": True}, "fallback": "x"},
            "data.price must be a number",
        ),
        (
            {
                "card": "progress",
                "data": {"label": "a", "value": 101, "status": "done"},
                "fallback": "x",
            },
            "<= 100",
        ),
        (
            {
                "card": "progress",
                "data": {"label": "a", "value": 1, "status": "later"},
                "fallback": "x",
            },
            "data.status must be one of",
        ),
        ({"card": "person", "data": {"name": "A", "age": 3}, "fallback": "x"}, "not allowed"),
        ({"card": "compare", "data": {"columns": [], "rows": []}, "fallback": "x"}, "items"),
    ],
)
def test_invalid_input_returns_error(args: dict, needle: str) -> None:
    out = json.loads(RenderCardTool().invoke(json.dumps(args), _CTX))
    assert needle in out["error"]


def test_non_object_arguments_are_rejected() -> None:
    assert "error" in json.loads(RenderCardTool().invoke("[1]", _CTX))
    assert "error" in json.loads(RenderCardTool().invoke("{", _CTX))


def test_schema_lists_every_kind_and_requires_fallback() -> None:
    params = RenderCardTool().get_schema()["function"]["parameters"]
    assert params["properties"]["card"]["enum"] == list(CARD_DATA_SCHEMAS)
    assert params["required"] == ["card", "data", "fallback"]
    # A plain object: union schemas break harnesses (pi) that merge branches.
    assert params["properties"]["data"]["type"] == "object"
    assert "anyOf" not in params["properties"]["data"]
    description = RenderCardTool().description()
    assert all(kind in description for kind in CARD_DATA_SCHEMAS)
    assert "sources {items: [{title: string, url: string, snippet?: string}]}" in description


def test_dispatches_locally() -> None:
    assert should_dispatch_locally("render_card")


def _spec() -> AgentSpec:
    return AgentSpec(spec_version=1, skills_filter="none")


def test_registered_for_super_chat_not_helpers() -> None:
    mode = {"omnigent.context.mode": "superside-chat"}
    assert "render_card" in ToolManager(_spec(), labels=mode).get_tool_names()
    helper = {**mode, "omnigent.subagent": "researcher"}
    assert "render_card" not in ToolManager(_spec(), labels=helper).get_tool_names()
    assert "render_card" not in ToolManager(_spec()).get_tool_names()


def test_ask_is_not_a_render_card_kind_there_is_one_way_to_ask() -> None:
    assert "ask" not in CARD_DATA_SCHEMAS
    out = _call(
        card="ask",
        data={"question": "Which?", "options": [{"id": "a", "label": "A"}]},
        fallback="Which?",
    )
    assert "card must be one of" in out["error"]
    assert (
        "ask_clarification"
        not in RenderCardTool().get_schema()["function"]["parameters"]["properties"]["card"][
            "enum"
        ]
    )
