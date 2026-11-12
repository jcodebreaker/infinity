"""Every template exports exactly: native PowerPoint objects only, fonts embedded, layout clean.

Runs the real ``superchat-deck-export`` helper in a headless Chromium on decks built by the kit
from the sample slides, so a template or layout change that would make the PowerPoint differ from
the page (a picture where a shape should be, an unembedded font, content in the footer band) fails
here. Skipped when no Chromium is installed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from omnigent.superchat.decks import kit

HELPER = Path(__file__).resolve().parents[4] / "host/bin/superchat-deck-export"
SAMPLE = (kit.KIT_DIR / "sample-slides.html").read_text("utf-8")


def _chromium_available() -> bool:
    spec_dir = str(HELPER.parent)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.machinery as m, importlib.util as u;"
            f"l=m.SourceFileLoader('h','{HELPER}');s=u.spec_from_loader('h',l);x=u.module_from_spec(s);"
            "l.exec_module(x);x.find_chromium()",
        ],
        capture_output=True,
        cwd=spec_dir,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not HELPER.is_file() or not _chromium_available(), reason="Chromium or the helper is missing"
)


def _run(fmt: str, html: Path, out: Path | None = None) -> dict[str, object]:
    args = [sys.executable, str(HELPER), "--format", fmt, "--html", str(html)]
    if out is not None:
        args += ["--out", str(out)]
    done = subprocess.run(args, capture_output=True, text=True, timeout=170, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module", params=sorted(kit.templates()))
def deck(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[str, Path]:
    path = tmp_path_factory.mktemp(request.param) / "sample.deck.html"
    path.write_text(kit.build_deck(request.param, "Quarterly review", SAMPLE), "utf-8")
    return request.param, path


def test_layout_is_clean_by_construction(deck: tuple[str, Path]) -> None:
    result = _run("lint", deck[1])
    assert result["ok"] is True and result["slides"] == 11
    assert result["issues"] == []


def test_pptx_is_native_with_embedded_fonts_and_no_pictures(
    deck: tuple[str, Path], tmp_path: Path
) -> None:
    template, path = deck
    out = tmp_path / "deck.pptx"
    assert _run("pptx", path, out)["ok"] is True
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        slides = [n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        xml = {n: z.read(n).decode("utf-8") for n in slides}
        presentation = z.read("ppt/presentation.xml").decode("utf-8")
    assert len(slides) == 11
    # shapes and text only: not one picture, so nothing a person edits is a screenshot
    assert [n for n in names if n.startswith("ppt/media/") and not n.endswith("/")] == []
    assert all("<p:pic>" not in x for x in xml.values())
    # every authored text is a real text run
    runs = {t for x in xml.values() for t in re.findall(r"<a:t>([^<]*)</a:t>", x)}
    for expected in ("Shipping got faster", "Three things moved", "Approve weekly releases"):
        assert expected.upper() in runs or expected in runs
    # the template's fonts travel inside the file, one embedded face per family
    families = {f["family"] for f in kit.templates()[template].fonts}
    embedded = set(re.findall(r'<p:embeddedFont><p:font typeface="([^"]+)"', presentation))
    assert embedded == families
    assert len([n for n in names if n.startswith("ppt/fonts/") and not n.endswith("/")]) == len(
        families
    )


def test_pdf_is_vector_with_one_page_per_slide(deck: tuple[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "deck.pdf"
    assert _run("pdf", deck[1], out)["ok"] is True
    data = out.read_bytes()
    assert len(re.findall(rb"/Type\s*/Page[^s]", data)) == 11
    assert b"/FontFile" in data
    # only a chart's canvas is a picture in a PDF (its pixels and their alpha mask)
    assert len(re.findall(rb"/Subtype\s*/Image", data)) <= 2 * SAMPLE.count("<canvas")
