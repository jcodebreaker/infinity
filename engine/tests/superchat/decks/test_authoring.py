"""Decks: the kit (templates, assembly, static checks) and the deck_new / deck_check handlers."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest

from omnigent.superchat.decks import kit
from omnigent.superchat.decks.authoring import handle_authoring_tool
from omnigent.superchat.decks.feature import DECKS_FEATURE
from omnigent.superchat.decks.handlers import HELPER_ENV
from omnigent.superchat.decks.tools import (
    LOOK_SOURCES,
    DeckCheckTool,
    DeckNewTool,
    DeckThemeSetTool,
)

from .look_fakes import helper_ctx

SAMPLE = (kit.KIT_DIR / "sample-slides.html").read_text("utf-8")


def _labels() -> dict[str, str]:
    from omnigent.context.labels import CONTEXT_MODE_LABEL, SUPERSIDE_CHAT_MODE_VALUE

    return {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}


def _slides_in(deck: str) -> str:
    return deck.split(kit.SLOTS_OPEN, 1)[1].split(kit.SLOTS_CLOSE, 1)[0]


def test_theme_library_has_licenses_metadata_and_embeddable_fonts() -> None:
    templates = kit.templates()
    assert len(templates) == 18
    assert {"corporate-clean", "minimal-white", "editorial-tri-tone"} <= set(templates)
    for template in templates.values():
        assert (template.directory / "LICENSE").read_text("utf-8").startswith("MIT License")
        assert template.category and template.mode in {"light", "dark"}
        for font in template.fonts:
            assert (kit.FONTS_DIR / str(font["file"])).stat().st_size > 10_000
            assert (kit.FONTS_DIR / str(font["license"])).read_text("utf-8").strip()


@pytest.mark.parametrize("template", sorted(kit.templates()))
def test_assembled_deck_keeps_the_framework_and_embeds_fonts(template: str) -> None:
    deck = kit.build_deck(template, "Q3 <review> & plans", SAMPLE)
    assert deck.startswith("<!doctype html>")
    assert '<html lang="en" data-superchat-deck-protocol="1">' in deck
    assert "<title>Q3 &lt;review&gt; &amp; plans</title>" in deck
    # the framework: one stage, print rules, protocol, export-aware fit()
    assert 'class="deck-stage" id="deck-stage"' in deck and "@media print" in deck
    assert "superchat:slide-state" in deck and "data-superchat-export" in deck
    # the theme and slides went into their slots; no slot comment is left behind
    assert "SLOT:" not in deck.replace("SLOT: theme tokens", "")
    assert kit.SLOTS_OPEN in deck and kit.SLOTS_CLOSE in deck
    assert _slides_in(deck).count('<section class="slide') == 11
    # fonts are data URIs (the viewer's CSP blocks remote fonts); nothing is fetched from the web
    assert "data:font/ttf;base64," in deck
    assert not re.search(r"https?://fonts\.", deck)
    assert not kit._EXTERNAL_RE.search(deck.split("</style>")[0].split("<style>", 1)[-1] + SAMPLE)


def test_sample_slides_pass_the_static_checks_and_every_text_block_has_an_id() -> None:
    assert kit.check_slides(SAMPLE) == ([], [])
    ids = kit._ID_RE.findall(SAMPLE)
    assert len(ids) == len(set(ids)) > 50


def test_static_checks_report_what_a_slide_must_fix() -> None:
    errors, warnings = kit.check_slides(
        '<section class="slide t-a"><h1>No id</h1><img src="https://x.test/a.png">'
        "<script>1</script></section>"
    )
    joined = " ".join(errors)
    assert "data-screen-label" in joined and "data-superchat-id" in joined
    assert "<script>" in joined and "web" in joined
    assert warnings and "active" in warnings[0]
    dup, _ = kit.check_slides(
        '<section class="slide active" data-screen-label="01 A" data-superchat-id="a">'
        '<p data-superchat-id="x">1</p><p data-superchat-id="x">2</p>'
        '<p data-superchat-id="Bad_Id">3</p></section>'
    )
    assert any('"x" is used more than once' in e for e in dup)
    assert any("Bad_Id" in e for e in dup)
    assert kit.check_slides("<p>nothing</p>")[0]


def test_unknown_template_is_refused() -> None:
    with pytest.raises(kit.KitError):
        kit.build_deck("nope", "x", SAMPLE)


def test_tools_are_offered_to_the_muse_only() -> None:
    names = {t.name() for t in DECKS_FEATURE.tools(_labels(), None)}  # type: ignore[arg-type]
    assert names == {"deck_new", "deck_check", "deck_export", "deck_themes", "deck_theme_set"}
    assert DECKS_FEATURE.tools({}, None) == []  # type: ignore[arg-type]
    new = DeckNewTool().get_schema()["function"]["parameters"]
    # template and look_from are optional: the engine chooses without a template (ADR 0026)
    assert new["required"] == ["path", "title"]
    assert new["properties"]["look_from"]["enum"] == list(LOOK_SOURCES)
    assert "ask" not in LOOK_SOURCES and "preference" not in LOOK_SOURCES
    assert new["properties"]["template"]["enum"] == list(kit.templates())
    assert DeckCheckTool().get_schema()["function"]["parameters"]["required"] == ["path"]
    setter = DeckThemeSetTool().get_schema()["function"]["parameters"]
    assert setter["required"] == ["path", "theme_id"]
    assert setter["properties"]["theme_id"]["enum"] == list(kit.templates())
    for name in ("deck_new", "deck_check", "deck_themes", "deck_theme_set"):
        assert name in DECKS_FEATURE.handlers


def _fake_helper(tmp_path: Path, issues: list[dict[str, object]] | None) -> Path:
    """A helper that answers `--format lint` with `issues` (or fails when `issues` is None)."""
    script = tmp_path / "fake-helper"
    if issues is None:
        body = 'print(\'{"ok": false, "error": "Chromium is not installed on this host"}\')'
    else:
        body = f"print({json.dumps(json.dumps({'ok': True, 'slides': 8, 'issues': issues}))})"
    script.write_text(f"#!/usr/bin/env python3\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(root))
    monkeypatch.setenv(HELPER_ENV, str(_fake_helper(tmp_path, [])))
    return root


async def test_deck_new_writes_the_deck_and_reports_a_clean_layout(workspace: Path) -> None:
    out = json.loads(
        await handle_authoring_tool(
            "deck_new",
            {
                "path": "your_files/q3.deck.html",
                "template": "blue-professional",
                "look_from": "background",
                "title": "Q3",
                "slides": SAMPLE,
            },
            helper_ctx(),
        )
    )
    assert out["ok"] is True and out["written"] is True and out["layout_checked"] is True
    target = workspace / "your_files" / "q3.deck.html"
    assert out["path"] == str(target.resolve())
    assert _slides_in(target.read_text("utf-8")).count('<section class="slide') == 11
    assert "artifact_save" in out["next"]


async def test_deck_new_without_slides_returns_the_layout_menu_and_writes_nothing(
    workspace: Path,
) -> None:
    out = json.loads(
        await handle_authoring_tool(
            "deck_new",
            {
                "path": "d.deck.html",
                "template": "magazine-mono",
                "look_from": "background",
                "title": "T",
            },
            helper_ctx(),
        )
    )
    # The record only: the layouts are the deck skill (ADR 0031), never a tool result.
    assert out["written"] is False and "layouts" not in out and out["deck"]["title"] == "T"
    assert not (workspace / "d.deck.html").exists()


async def test_deck_new_refuses_bad_input_and_never_overwrites(workspace: Path) -> None:
    base = {"template": "magazine-mono", "look_from": "background", "title": "T", "slides": SAMPLE}

    async def new(args: dict[str, str]) -> dict[str, object]:
        return json.loads(await handle_authoring_tool("deck_new", args, helper_ctx()))

    bad_name = await new({**base, "path": "d.html"})
    assert ".deck.html" in bad_name["error"]
    outside = await new({**base, "path": "/etc/x.deck.html"})
    assert "workspace" in outside["error"]
    broken = await new({**base, "path": "d.deck.html", "slides": "<p>x</p>"})
    assert broken["written"] is False and broken["errors"]
    assert not (workspace / "d.deck.html").exists()
    first = await new({**base, "path": "d.deck.html"})
    assert first["written"] is True
    again = await new({**base, "path": "d.deck.html"})
    assert "already exists" in again["error"]


async def test_deck_check_lists_layout_errors_with_slide_and_id(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "d.deck.html").write_text(kit.build_deck("magazine-mono", "T", SAMPLE), "utf-8")
    issues = [
        {
            "slide": 2,
            "label": "02 Why",
            "id": "why-statement",
            "severity": "error",
            "code": "footer-rail",
            "message": "This content runs into the footer band; move it up or split the slide.",
        },
        {
            "slide": 3,
            "label": "03 Ideas",
            "id": "",
            "severity": "warning",
            "code": "small-text",
            "message": "Text is 14px.",
        },
        {
            "slide": 1,
            "label": "01 Cover",
            "id": "glow",
            "severity": "note",
            "code": "picture-layer",
            "message": "gradient becomes a picture layer.",
        },
    ]
    monkeypatch.setenv(HELPER_ENV, str(_fake_helper(tmp_path, issues)))
    out = json.loads(await handle_authoring_tool("deck_check", {"path": "d.deck.html"}))
    assert out["ok"] is False
    assert out["errors"] == [
        "Slide 2 (Why) [why-statement]: This content runs into the footer band; "
        "move it up or split the slide."
    ]
    assert out["warnings"] == ["Slide 3 (Ideas): Text is 14px."]
    assert out["notes"][0].startswith("Slide 1 (Cover) [glow]")
    assert "deck_check again" in out["next"]


async def test_deck_check_still_checks_structure_when_chromium_is_missing(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = kit.build_deck("magazine-mono", "T", SAMPLE).replace(
        ' data-superchat-id="why-statement"', ""
    )
    broken = broken.replace('data-superchat-id="why"', 'data-superchat-id="cover"')
    (workspace / "d.deck.html").write_text(broken, "utf-8")
    monkeypatch.setenv(HELPER_ENV, str(_fake_helper(tmp_path, None)))
    out = json.loads(await handle_authoring_tool("deck_check", {"path": "d.deck.html"}))
    assert out["layout_checked"] is False and "Chromium" in out["layout_note"]
    assert out["ok"] is False and any("more than once" in e for e in out["errors"])


async def test_deck_check_unknown_file(workspace: Path) -> None:
    out = json.loads(await handle_authoring_tool("deck_check", {"path": "missing.deck.html"}))
    assert "not found" in out["error"].lower()


def test_every_theme_has_a_small_thumbnail() -> None:
    from PIL import Image

    for theme in kit.theme_dictionary():
        path = kit.PREVIEWS_DIR / f"{theme['id']}.webp"
        assert path.stat().st_size < 40_000
        with Image.open(path) as image:
            assert image.size == (640, 360)
        with Image.open(path) as image:  # no baked-in letterbox: the corners are the slide
            corners = [image.convert("L").getpixel(xy) for xy in ((0, 0), (639, 0), (0, 359))]
        assert (max(corners) < 90) == (theme["mode"] == "dark")
    gallery = {t["id"]: t for t in kit.theme_gallery()}
    assert all(t["preview"].startswith("data:image/webp;base64,") for t in gallery.values())


async def test_deck_themes_returns_the_dictionary_and_a_restrained_default() -> None:
    out = json.loads(await handle_authoring_tool("deck_themes", {}))
    ids = [t["id"] for t in out["themes"]]
    assert ids == [t["id"] for t in kit.theme_dictionary()] and len(ids) == 18
    assert out["default"] == "corporate-clean"
    keys = {"id", "name", "tagline", "mood", "category", "mode", "best_for"}
    assert set(out["themes"][0]) == keys
    assert {t["category"] for t in out["themes"]} == {"professional", "editorial", "bold", "dark"}
    assert {t["mode"] for t in out["themes"]} == {"light", "dark"}


async def test_deck_theme_set_restyles_the_file_and_keeps_the_slides(workspace: Path) -> None:
    deck = kit.build_deck("corporate-clean", "T", SAMPLE)
    (workspace / "d.deck.html").write_text(deck, "utf-8")
    args = {"path": "d.deck.html", "theme_id": "nord"}
    out = json.loads(await handle_authoring_tool("deck_theme_set", args))
    assert out["ok"] is True and out["changed"] is True and out["previous"] == "corporate-clean"
    written = (workspace / "d.deck.html").read_text("utf-8")
    assert written == kit.build_deck("nord", "T", SAMPLE)
    again = json.loads(await handle_authoring_tool("deck_theme_set", args))
    assert again == {"ok": True, "changed": False, "theme": "nord", "path": again["path"]}


async def test_deck_theme_set_refuses_bad_input(workspace: Path) -> None:
    (workspace / "d.deck.html").write_text("<html><body>hand made</body></html>", "utf-8")
    bad_theme = await handle_authoring_tool(
        "deck_theme_set", {"path": "d.deck.html", "theme_id": "x"}
    )
    assert "theme_id must be one of" in json.loads(bad_theme)["error"]
    hand_made = await handle_authoring_tool(
        "deck_theme_set", {"path": "d.deck.html", "theme_id": "nord"}
    )
    assert "ask Superchat" in json.loads(hand_made)["error"]
    missing = await handle_authoring_tool(
        "deck_theme_set", {"path": "no.deck.html", "theme_id": "nord"}
    )
    assert "not found" in json.loads(missing)["error"].lower()


def test_thumbnails_of_themes_offered_together_read_apart() -> None:
    """Two themes the Muse may offer side by side (different category or mode) never share a
    near-identical thumbnail: the mean per-pixel difference at tile scale stays clear of zero."""
    from itertools import combinations

    from PIL import Image, ImageChops, ImageStat

    themes = kit.templates()
    small = {
        t: Image.open(kit.PREVIEWS_DIR / f"{t}.webp").convert("RGB").resize((64, 36))
        for t in themes
    }
    for a, b in combinations(themes, 2):
        if (themes[a].category, themes[a].mode) == (themes[b].category, themes[b].mode):
            continue
        diff = sum(ImageStat.Stat(ImageChops.difference(small[a], small[b])).mean) / 3
        assert diff > 6, (a, b, diff)
