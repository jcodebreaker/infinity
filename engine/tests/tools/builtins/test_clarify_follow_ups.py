"""Tests for the ``ask_clarification`` and ``suggest_follow_ups`` card tools."""

import json

import pytest

import omnigent.superchat.decks  # noqa: F401  (registers the deck-theme preview kind)
from omnigent.runner.tool_dispatch import should_dispatch_locally
from omnigent.spec.types import AgentSpec
from omnigent.superchat.cards.tools import (
    AskClarificationTool,
    SuggestFollowUpsTool,
    build_clarification,
    build_follow_ups,
)
from omnigent.superchat.transcript.blocks import project_items
from omnigent.tools.base import ToolContext
from omnigent.tools.manager import ToolManager

_CTX = ToolContext(task_id="", conversation_id="", agent_id="")


def _ask(**args: object) -> dict:
    return json.loads(AskClarificationTool().invoke(json.dumps(args), _CTX))


def _follow(**args: object) -> dict:
    return json.loads(SuggestFollowUpsTool().invoke(json.dumps(args), _CTX))


def test_clarification_builds_an_ask_card_with_option_ids() -> None:
    out = _ask(question="  Which quarter? ", options=["Q1", "Q2", " Q3 "])
    assert out["type"] == "card" and out["card"] == "ask"
    assert out["data"] == {
        "question": "Which quarter?",
        "options": [
            {"id": "q1", "label": "Q1"},
            {"id": "q2", "label": "Q2"},
            {"id": "q3", "label": "Q3"},
        ],
    }
    assert out["fallback"] == "Which quarter?\n\n- Q1\n- Q2\n- Q3"
    assert out["ends_turn"] is True


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"options": ["a", "b"]}, "question"),
        ({"question": " ", "options": ["a", "b"]}, "question"),
        ({"question": "q", "options": ["only"]}, "2-5"),
        ({"question": "q", "options": list("abcdef")}, "2-5"),
        ({"question": "q"}, "options"),
        ({"question": "q", "options": ["a", 3]}, "string"),
        ({"question": "q", "options": ["a", ""]}, "empty"),
        ({"question": "q", "options": ["a", "A"]}, "repeats"),
        ({"question": "q", "options": ["a", "x" * 61]}, "shorten"),
    ],
)
def test_clarification_rejects_bad_input(args: dict, needle: str) -> None:
    assert needle in build_clarification(args)["error"]


def test_follow_ups_builds_a_chip_card_that_leaves_the_turn_open() -> None:
    out = _follow(suggestions=["Compare with last year", " Show by region "])
    assert out["card"] == "follow_ups" and out["type"] == "card"
    assert out["data"] == {"suggestions": ["Compare with last year", "Show by region"]}
    assert out["fallback"] == "- Compare with last year\n- Show by region"
    # chips never end the turn: the model ends it (ADR 0029)
    assert "ends_turn" not in out


@pytest.mark.parametrize(
    "suggestions", [[], ["a", "b", "c", "d"], ["ok", ""], "nope", ["x" * 121], ["Same", "same"]]
)
def test_follow_ups_rejects_bad_input(suggestions: object) -> None:
    assert "error" in build_follow_ups({"suggestions": suggestions})


def test_non_object_arguments_are_rejected() -> None:
    for tool in (AskClarificationTool(), SuggestFollowUpsTool()):
        assert "error" in json.loads(tool.invoke("[1]", _CTX))
        assert "error" in json.loads(tool.invoke("{", _CTX))


def test_schemas_bound_the_counts_and_say_when_to_use_them() -> None:
    ask = AskClarificationTool().get_schema()["function"]
    options = ask["parameters"]["properties"]["options"]
    assert (options["minItems"], options["maxItems"]) == (2, 5)
    assert ask["parameters"]["required"] == ["question", "options"]
    assert "genuinely ambiguous" in ask["description"] and "just proceed" in ask["description"]
    follow = SuggestFollowUpsTool().get_schema()["function"]
    suggestions = follow["parameters"]["properties"]["suggestions"]
    assert (suggestions["minItems"], suggestions["maxItems"]) == (1, 3)
    # Shown as quiet chips under the answer; no "last step" (ADR 0029: chips end no turn).
    assert "quiet chips under your answer" in follow["description"]
    assert "never for filler" in follow["description"]
    assert "last step" not in follow["description"]


def test_dispatch_and_registration_follow_render_card() -> None:
    assert should_dispatch_locally("ask_clarification")
    assert should_dispatch_locally("suggest_follow_ups")
    spec = AgentSpec(spec_version=1, skills_filter="none")
    mode = {"omnigent.context.mode": "superside-chat"}
    names = ToolManager(spec, labels=mode).get_tool_names()
    assert {"ask_clarification", "suggest_follow_ups"} <= set(names)
    helper = {**mode, "omnigent.subagent": "researcher"}
    helper_names = ToolManager(spec, labels=helper).get_tool_names()
    assert not {"ask_clarification", "suggest_follow_ups"} & set(helper_names)
    plain = ToolManager(spec).get_tool_names()
    assert not {"ask_clarification", "suggest_follow_ups"} & set(plain)


def _call(name: str, call_id: str) -> dict:
    return {
        "id": f"i-{call_id}",
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": "{}",
        "created_at": 100,
    }


def _output(call_id: str, payload: dict) -> dict:
    return {
        "id": f"o-{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(payload),
    }


def test_transcript_projects_both_calls_as_card_blocks() -> None:
    items = [
        _call("ask_clarification", "k1"),
        _output("k1", build_clarification({"question": "Which?", "options": ["A", "B"]})),
        _call("suggest_follow_ups", "k2"),
        _output("k2", build_follow_ups({"suggestions": ["Go deeper"]})),
    ]
    first, second = project_items(items)
    assert first["blocks"][0]["card"]["card"] == "ask"
    assert second["blocks"][0]["card"]["card"] == "follow_ups"
    assert second["blocks"][0]["card"]["data"] == {"suggestions": ["Go deeper"]}
    assert "pending" not in first["blocks"][0]


def test_transcript_skips_unfinished_and_failed_calls() -> None:
    # Instant tools never show a skeleton; a rejected call shows nothing.
    assert project_items([_call("ask_clarification", "k1")]) == []
    failed = [_call("suggest_follow_ups", "k2"), _output("k2", build_follow_ups({}))]
    assert project_items(failed) == []


def test_hidden_control_characters_are_stripped_from_buttons() -> None:
    # Zero-width space, RTL override and a BOM can hide or reorder what a button says.
    out = build_follow_ups({"suggestions": ["Show\u200b it\u202e by region\ufeff"]})
    assert out["data"]["suggestions"] == ["Show it by region"]
    ask = build_clarification({"question": "Q?", "options": ["A\u200b", "B\u2066"]})
    assert [o["label"] for o in ask["data"]["options"]] == ["A", "B"]
    # Stripping must not let two look-alike options through as distinct.
    assert (
        "repeats" in build_clarification({"question": "Q?", "options": ["A", "A\u200b"]})["error"]
    )


def test_text_after_a_follow_ups_card_is_the_answer_and_the_chips_stay_last() -> None:
    items = [
        _msg("u1", "user", "How is the backlog?"),
        _msg("a1", "assistant", "It fell from 84 to 61."),
        _call("suggest_follow_ups", "k1"),
        _output("k1", build_follow_ups({"suggestions": ["By auditor"]})),
        _msg("a2", "assistant", "It fell from 84 to 61, mostly in March."),
    ]
    out = project_items(items)
    assert [m["id"] for m in out] == ["u1", "a2", "i-k1"]
    # The chips are the last message, which is what the client keys "latest answer" on.
    assert out[-1]["blocks"][0]["card"]["card"] == "follow_ups"


def test_a_rejected_follow_ups_call_leaves_the_turns_last_text_as_its_answer() -> None:
    items = [
        _msg("u1", "user", "Hi"),
        _msg("a1", "assistant", "First."),
        _call("suggest_follow_ups", "k1"),
        _output("k1", build_follow_ups({})),
        _msg("a2", "assistant", "Second."),
    ]
    assert [m["id"] for m in project_items(items)] == ["u1", "a2"]


def test_text_after_a_clarification_card_is_shown_too() -> None:
    items = [
        _msg("u1", "user", "Pull the numbers"),
        _msg("a1", "assistant", "I need one thing first."),
        _call("ask_clarification", "k1"),
        _output("k1", build_clarification({"question": "Which?", "options": ["A", "B"]})),
        _msg("a2", "assistant", "Let me know."),
    ]
    assert [m["id"] for m in project_items(items)] == ["u1", "i-k1", "a2"]


def _msg(item_id: str, role: str, text: str) -> dict:
    kind = "output_text" if role == "assistant" else "input_text"
    return {
        "id": item_id,
        "type": "message",
        "role": role,
        "content": [{"type": kind, "text": text}],
        "created_at": 100,
    }


def _themed(*ids: str) -> list[dict]:
    return [{"label": f"Look {i}", "preview": {"kind": "deck-theme", "id": i}} for i in ids]


def test_clarification_options_may_preview_a_deck_theme() -> None:
    out = _ask(question="Which look?", options=_themed("corporate-clean", "nord", "bauhaus"))
    assert out["card"] == "ask"
    # a previewed option's id is its preview's: the pick names the theme by id
    assert out["data"]["options"][1] == {
        "id": "nord",
        "label": "Look nord",
        "preview": {"kind": "deck-theme", "id": "nord"},
    }
    # Plain and previewed options mix; a plain option carries no preview key.
    mixed = _ask(question="Which?", options=["Plain", *_themed("nord")])
    assert "preview" not in mixed["data"]["options"][0]


def test_clarification_rejects_unknown_preview_ids_and_kinds() -> None:
    assert (
        "not a known deck-theme" in _ask(question="Q?", options=_themed("nord", "nope"))["error"]
    )
    bad_kind = [{"label": "A", "preview": {"kind": "photo", "id": "x"}}, "B"]
    assert "preview.kind" in _ask(question="Q?", options=bad_kind)["error"]
    assert (
        "label and preview" in _ask(question="Q?", options=[{"label": "A", "x": 1}, "B"])["error"]
    )


def test_clarification_refuses_look_alike_deck_themes() -> None:
    """The incident: three light professional themes are one choice, not three."""
    alike = _themed("corporate-clean", "arctic-cool", "blue-professional")
    assert "look alike" in _ask(question="Which look?", options=alike)["error"]
    distinct = _themed("corporate-clean", "editorial-serif", "tokyo-night")
    assert _ask(question="Which look?", options=distinct)["card"] == "ask"


def test_transcript_carries_the_option_previews() -> None:
    card = build_clarification({"question": "Which look?", "options": _themed("nord", "bauhaus")})
    items = [_call("ask_clarification", "k1"), _output("k1", card)]
    (message,) = project_items(items)
    options = message["blocks"][0]["card"]["data"]["options"]
    assert [o["preview"]["id"] for o in options] == ["nord", "bauhaus"]


def test_clarification_keeps_the_models_option_ids_and_makes_the_rest_distinct() -> None:
    """The web sends the picked option's id back with the label (``context.card_answer``)."""
    out = _ask(
        question="Data?", options=[{"id": "mock", "label": "Use mock data"}, "Real!", "Real"]
    )
    assert [o["id"] for o in out["data"]["options"]] == ["mock", "real", "real-2"]
    assert "id" in _ask(question="Q?", options=[{"id": "a b", "label": "A"}, "B"])["error"]
    clash = _ask(question="Q?", options=[{"id": "x", "label": "A"}, {"id": "x", "label": "B"}])
    assert "repeats" in clash["error"]
    themed = _ask(question="Q?", options=[{"id": "a", **_themed("nord")[0]}, "Bauhaus"])
    assert [o["id"] for o in themed["data"]["options"]] == ["nord", "bauhaus"]


def test_a_clarification_card_records_the_round_it_asks_in() -> None:
    args = {"question": "Data?", "options": ["Mock", "Real"]}
    card = build_clarification(args, "r1")
    assert card["round_id"] == "r1" and "task_id" not in card
    assert "round_id" not in build_clarification(args)
    # the card the web draws does not carry it
    items = [_call("ask_clarification", "k1"), _output("k1", build_clarification(args, "r1"))]
    (message,) = project_items(items)
    assert "round_id" not in message["blocks"][0]["card"]
