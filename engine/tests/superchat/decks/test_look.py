"""A new deck's look (ADR 0026): any valid template is built and ``look_from`` recorded; with no
template the theme picked on the task's card is built, else the engine chooses; never a card."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from omnigent.superchat.decks import kit
from omnigent.superchat.decks.authoring import handle_authoring_tool
from omnigent.superchat.decks.handlers import HELPER_ENV
from omnigent.superchat.decks.look import (
    CHOSEN,
    HELPER_REFUSAL,
    MODEL,
    PICKED,
    choose_look,
    rank,
    remembered_themes,
    settle_look,
)
from omnigent.superchat.feature import HandlerCtx

from .look_fakes import SESSION, ctx, helper_ctx, view

SAMPLE = (kit.KIT_DIR / "sample-slides.html").read_text("utf-8")


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    helper = tmp_path / "fake-helper"
    report = json.dumps(json.dumps({"ok": True, "slides": 1, "issues": []}))
    helper.write_text(f"#!/usr/bin/env python3\nprint({report})\n")
    helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(root))
    monkeypatch.setenv(HELPER_ENV, str(helper))
    return root


async def _new(
    look_from: str | None, template: str | None, call_ctx, title: str = "Sales"
) -> dict:
    args = {"path": "d.deck.html", "title": title, "slides": SAMPLE}
    if template is not None:
        args["template"] = template
    if look_from is not None:
        args["look_from"] = look_from
    return json.loads(await handle_authoring_tool("deck_new", args, call_ctx))


def _kept(out: dict, template: str, workspace: Path, look_from: str) -> bool:
    """Built in the model's template, with no look note, recording *look_from*."""
    built = out.get("written") is True and out["template"] == template
    built = built and kit.deck_theme_id((workspace / "d.deck.html").read_text("utf-8")) == template
    (workspace / "d.deck.html").unlink()
    return built and "look_note" not in out and out["deck"]["look_from"] == look_from


def _chosen(out: dict, workspace: Path) -> str:
    """Built in a look the engine chose; returns it."""
    assert out.get("written") is True and "card" not in out
    assert (
        "Theme control changes the look" in out["look_note"] and out["deck"]["look_from"] == CHOSEN
    )
    theme = out["template"]
    assert kit.deck_theme_id((workspace / "d.deck.html").read_text("utf-8")) == theme
    assert kit.templates()[theme].name in out["look_note"]
    (workspace / "d.deck.html").unlink()
    return theme


async def test_an_explicit_template_is_built_and_look_from_is_recorded(workspace: Path) -> None:
    """The live failure: Swiss Grid picked before tasks existed, nothing on record, and the
    engine built Arctic Cool; the model spent 22 s restyling it back."""
    unread = ctx(None)  # no task view: any read would find nothing
    for look_from, recorded in (
        ("picked", "picked"),
        ("named", "named"),
        ("you_choose", "you_choose"),
        ("background", "background"),
        (None, MODEL),
        ("ask", MODEL),
        ("preference", MODEL),
    ):
        out = await _new(look_from, "swiss-grid", unread)
        assert _kept(out, "swiss-grid", workspace, recorded), look_from
    # what the person said does not overrule the model's template
    out = await _new("named", "bauhaus", ctx(view("make it dark")))
    assert _kept(out, "bauhaus", workspace, "named")


async def test_an_explicit_template_needs_no_server_read() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected read: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://srv")
    call = HandlerCtx(tool_name="deck_new", server_client=client, conversation_id=SESSION)
    look = await settle_look(call, "picked", "swiss-grid", "Sales")
    assert (look.theme, look.look_from, look.refusal, look.chosen) == (
        "swiss-grid",
        "picked",
        None,
        False,
    )


async def test_no_template_is_chosen_and_says_why() -> None:
    call = ctx(view("Create some presentation about Acme vs Globex bank results"))
    for look_from in (None, "named", "ask"):
        look = await settle_look(call, look_from, None, "Acme vs Globex")
        assert look.chosen and look.theme in ("corporate-clean", "blue-professional")
        assert look.why == "no template was given"
    # a template the model names is built, whatever the message or look_from says
    look = await settle_look(call, "ask", "bauhaus", "Acme vs Globex")
    assert (look.theme, look.look_from, look.chosen) == ("bauhaus", MODEL, False)


async def test_no_template_builds_the_theme_picked_on_the_rounds_card(workspace: Path) -> None:
    """A pick is read by its option id (a deck-theme option's id is the theme id), never by
    its label. It is the answer the Round's message carried, so a later turn of the Round, or a
    Helper it started, reads it too."""
    picked = ctx(view("Nord", answer="Nord, the cool one", option_id="nord"))
    out = await _new(None, None, picked, title="Acme vs Globex")
    assert out["template"] == "nord" and out["deck"]["look_from"] == PICKED
    assert "look_note" not in out
    (workspace / "d.deck.html").unlink()
    # the Round's pick holds while the newest message is another (a Helper's brief, a notice)
    later = ctx(view("[System: worker/Deck saved a draft]", answer="Bauhaus", option_id="bauhaus"))
    look = await settle_look(later, None, None, "Acme vs Globex")
    assert (look.theme, look.look_from) == ("bauhaus", PICKED)
    # a label naming a theme is not a pick
    look = await settle_look(ctx(view("Nord", answer="Nord")), None, None, "Acme vs Globex")
    assert look.chosen


async def test_background_with_no_topic_and_no_template_is_the_one_refusal_left(
    workspace: Path,
) -> None:
    for look_from in (None, "named"):
        look = await settle_look(helper_ctx(), look_from, None, "Deck")
        assert look.refusal == HELPER_REFUSAL, look_from
    # a template is built even then, whatever look_from says
    for look_from in (None, "ask"):
        out = await _new(look_from, "corporate-clean", helper_ctx(), title="Deck")
        assert _kept(out, "corporate-clean", workspace, MODEL), look_from
    # with a topic, the look is chosen even in the background
    theme = _chosen(await _new(None, None, helper_ctx(), title="Q3 revenue review"), workspace)
    assert kit.templates()[theme].category == "professional"


async def test_no_template_builds_in_a_chosen_look(workspace: Path) -> None:
    """The ADR 0025 failure: "Create some presentation about Acme vs Globex" got a card and nothing."""
    call = ctx(view("Create some presentation about Acme vs Globex bank results"))
    theme = _chosen(await _new(None, None, call, title="Acme vs Globex"), workspace)
    assert theme in ("corporate-clean", "blue-professional")
    # the menu call (no slides) names the look to use and writes nothing
    menu = json.loads(
        await handle_authoring_tool(
            "deck_new", {"path": "d.deck.html", "title": "Q3 sales"}, ctx(view("a deck"))
        )
    )
    assert menu["written"] is False and "chosen for the topic" in menu["note"]
    assert menu["deck"]["look_id"] in menu["note"]
    assert not (workspace / "d.deck.html").exists()
    # no server: chosen from the title
    _chosen(await _new(None, None, None, title="Board finance review"), workspace)


async def test_the_result_carries_what_the_task_records(workspace: Path) -> None:
    out = await _new(None, None, ctx(view("a deck about Acme vs Globex")), title="Acme vs Globex")
    deck = out["deck"]
    assert deck["title"] == "Acme vs Globex" and deck["file"] == "d.deck.html"
    assert (
        deck["look_id"] == out["template"]
        and deck["look"] == kit.templates()[deck["look_id"]].name
    )
    assert deck["slides"] == SAMPLE.count('<section class="slide') > 0


async def test_remembered_taste_leads_where_it_fits(workspace: Path) -> None:
    taste = "- Likes decks in Swiss Grid; dark themes like Nord for tech talks"
    assert remembered_themes(taste) == ["swiss-grid", "nord"]
    # no topic word: the first remembered look
    assert (
        _chosen(
            await _new(None, None, ctx(view("a deck"), profile=taste), title="Deck"), workspace
        )
        == "swiss-grid"
    )
    # a business topic: the remembered look that fits it
    out = await _new(None, None, ctx(view(""), profile=taste), title="Q3 revenue")
    assert _chosen(out, workspace) == "swiss-grid"
    # a tech topic: Swiss Grid does not fit, Nord does
    out = await _new(None, None, ctx(view(""), profile=taste), title="API platform")
    assert _chosen(out, workspace) == "nord"


def _category(theme_id: str) -> str:
    return kit.templates()[theme_id].category


def test_choose_look_takes_the_first_of_the_ordering() -> None:
    assert choose_look("Seed pitch for investors") == "pitch-deck-vc"
    assert choose_look("a dark deck, maybe Nord", ["swiss-grid"]) == "nord"
    assert _category(choose_look("Our trip to Morocco: food and culture")) == "editorial"
    assert _category(choose_look("Platform architecture for engineering")) == "dark"
    assert choose_look("2026 sales") in ("corporate-clean", "blue-professional")
    assert choose_look("2026 sales") == choose_look("2026 sales")  # deterministic
    assert choose_look("", ["bauhaus"]) == "bauhaus"
    assert choose_look("", ["not-a-theme"]) == rank("", seed="")[0]


def test_one_ranking_orders_every_theme_by_fit() -> None:
    assert sorted(rank("anything")) == sorted(kit.templates())
    assert rank("a dark deck, maybe Nord")[0] == "nord"
    assert rank("Seed pitch for investors")[0] == "pitch-deck-vc"
    assert _category(rank("Q3 finance review for the board")[0]) == "professional"
    assert _category(rank("Product launch campaign")[0]) == "bold"
    # equal fits are ordered by the request, so the choice varies without being random
    assert len({rank("Hiring plan", seed=f"c1|{n}")[0] for n in range(12)}) > 1


@pytest.mark.parametrize(
    "hint",
    [
        "2026 sales",
        "Q3 revenue review",
        "budget for the board",
        "Sales 2026 Make a 5-slide deck about our 2026 sales from sales_2026.csv",
    ],
)
def test_a_business_deck_leads_with_a_professional_look(hint: str) -> None:
    for seed in ("a", "b", "c", "d"):
        assert rank(hint, seed=seed)[0] in ("corporate-clean", "blue-professional"), (hint, seed)
