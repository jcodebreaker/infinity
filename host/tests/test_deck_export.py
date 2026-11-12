"""Deck export: PPTX structure and PDF output from a real headless Chromium.

Open Design's fidelity tests (pptx-editable-fidelity, pptx-layered-background) assert on the
PPTX that its capture pipeline produces. The fake-DOM halves of those are ported to vitest in
``deck-export/*.test.ts``; this file is the other half, run against a real render: text runs stay
editable, shapes stay native, a gradient background is its own layer behind native content, and a
slide is never one picture. Skipped when no Chromium is installed.
"""

import importlib.machinery
import importlib.util
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("superchat_deck_export", str(HERE.parent / "bin" / "superchat-deck-export"))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)

FIXTURE = HERE.parent / "share" / "deck-export" / "fixtures" / "basic.deck.html"
EMU_PER_PX = 9144000 / 1920  # dom-to-pptx lays a 1920px slide on a 10in (9144000 EMU) page


def have_chromium() -> bool:
    try:
        helper.find_chromium()
        return True
    except helper.ExportError:
        return False


def slide_shapes(pptx: Path, number: int) -> list[dict]:
    """Every <p:sp>/<p:pic> of a slide in z-order with its geometry and runs."""
    with zipfile.ZipFile(pptx) as z:
        xml = z.read(f"ppt/slides/slide{number}.xml").decode("utf-8")
    shapes = []
    for match in re.finditer(r"<p:(sp|pic)>.*?</p:\1>", xml, re.S):
        body = match.group(0)
        off = re.search(r'<a:off x="(-?\d+)" y="(-?\d+)"/><a:ext cx="(\d+)" cy="(\d+)"', body)
        shapes.append(
            {
                "kind": match.group(1),
                "x": int(off.group(1)) if off else 0,
                "y": int(off.group(2)) if off else 0,
                "w": int(off.group(3)) if off else 0,
                "h": int(off.group(4)) if off else 0,
                "text": re.findall(r"<a:t>([^<]*)</a:t>", body),
                "runs": body.count("<a:r>"),
                "italic": 'i="1"' in body,
                "bold": 'b="1"' in body,
                "fonts": set(re.findall(r'<a:latin typeface="([^"]*)"', body)),
            }
        )
    return shapes


@unittest.skipUnless(have_chromium(), "Chromium is not installed")
class DeckExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.pptx = cls.dir / "basic.pptx"
        cls.pdf = cls.dir / "basic.pdf"
        cls.pptx_info = helper.run_export(helper.Chromium(), "pptx", FIXTURE, cls.pptx)
        cls.pdf_info = helper.run_export(helper.Chromium(), "pdf", FIXTURE, cls.pdf)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_reports_slide_count_and_authored_size(self):
        self.assertEqual(
            {k: self.pptx_info[k] for k in ("ok", "slides", "width", "height")},
            {"ok": True, "slides": 3, "width": 1920, "height": 1080},
        )

    def test_one_native_slide_per_html_slide_at_16_9(self):
        with zipfile.ZipFile(self.pptx) as z:
            names = z.namelist()
            presentation = z.read("ppt/presentation.xml").decode()
        self.assertEqual(len([n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]), 3)
        cx, cy = map(int, re.search(r'<p:sldSz cx="(\d+)" cy="(\d+)"', presentation).groups())
        self.assertAlmostEqual(cx / cy, 16 / 9, places=3)

    def test_text_stays_editable_text_never_a_picture_of_text(self):
        texts = [t for n in (1, 2, 3) for s in slide_shapes(self.pptx, n) for t in s["text"]]
        for expected in (
            "Q3 2026 REVIEW",
            "Shipping",
            "velocity",
            "doubled",
            "What moved, what stalled, and what we do next.",
            "Three numbers that matter",
            "Releases shipped",
            "One decision to make",
            "Approve the move to weekly releases.",
        ):
            self.assertIn(expected, texts)

    def test_italic_emphasis_is_preserved_as_italic(self):
        velocity = next(s for s in slide_shapes(self.pptx, 1) if s["text"] == ["velocity"])
        self.assertTrue(velocity["italic"])

    def test_shapes_are_native_and_no_slide_is_one_picture(self):
        for number in (1, 2, 3):
            shapes = slide_shapes(self.pptx, number)
            pictures = [s for s in shapes if s["kind"] == "pic"]
            native = [s for s in shapes if s["kind"] == "sp"]
            self.assertGreater(len(native), 4, f"slide {number} has too few native shapes")
            for picture in pictures:
                covers_slide = picture["w"] >= 9144000 * 0.98 and picture["h"] >= 5143500 * 0.98
                # A full-slide layer is allowed only as a background layer BEHIND native content.
                if covers_slide:
                    self.assertEqual(shapes.index(picture), 0, "background layer must be at the back")

    def test_gradient_background_is_one_layer_behind_native_content(self):
        shapes = slide_shapes(self.pptx, 1)
        pictures = [i for i, s in enumerate(shapes) if s["kind"] == "pic"]
        self.assertEqual(len(pictures), 1, "the radial glow is exactly one layered background")
        first_text = next(i for i, s in enumerate(shapes) if s["text"])
        self.assertLess(pictures[0], first_text)
        # The slides without a gradient carry no picture at all.
        for number in (2, 3):
            self.assertEqual([s for s in slide_shapes(self.pptx, number) if s["kind"] == "pic"], [])

    def test_geometry_is_one_to_one_with_the_authored_canvas(self):
        # `.slide` padding is 96px 120px, so the kicker sits at x=120px, y=96px (unscaled stage).
        kicker = next(s for s in slide_shapes(self.pptx, 1) if s["text"] == ["Q3 2026 REVIEW"])
        self.assertAlmostEqual(kicker["x"], 120 * EMU_PER_PX, delta=EMU_PER_PX * 2)
        self.assertAlmostEqual(kicker["y"], 96 * EMU_PER_PX, delta=EMU_PER_PX * 6)

    def test_fonts_are_named_not_substituted(self):
        fonts = set().union(*(s["fonts"] for n in (1, 2, 3) for s in slide_shapes(self.pptx, n)))
        self.assertTrue({"Georgia", "Helvetica"} <= fonts, fonts)

    def test_pdf_is_vector_one_page_per_slide(self):
        data = self.pdf.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(len(re.findall(rb"/Type\s*/Page[^s]", data)), 3)
        self.assertEqual(self.pdf_info["slides"], 3)
        # Page size comes from the deck's own @page (1920x1080 CSS px = 1440x810 pt).
        self.assertIn(b"/MediaBox [0 0 1440 810]", data)
        # Text is drawn with fonts (vector), not rasterised: the PDF embeds font programs.
        self.assertIn(b"/FontFile", data)

    def test_rich_text_inside_a_paragraph_stays_one_box_of_runs(self):
        html = FIXTURE.read_text().replace(
            '<p class="lead" data-superchat-id="s3-lead">Approve the move to weekly releases.</p>',
            '<p class="lead" data-superchat-id="s3-lead">Approve the <strong>move</strong> to '
            "<em>weekly</em> releases today.</p>",
        )
        src = self.dir / "rich.deck.html"
        src.write_text(html)
        out = self.dir / "rich.pptx"
        helper.run_export(helper.Chromium(), "pptx", src, out)
        box = next(s for s in slide_shapes(out, 3) if "move" in s["text"])
        self.assertEqual(box["text"], ["Approve the ", "move", " to ", "weekly", " releases today."])
        self.assertEqual(box["runs"], 5)

    def test_a_document_without_slides_is_refused_not_exported_as_a_page(self):
        src = self.dir / "page.html"
        src.write_text("<!doctype html><title>x</title><h1>Not a deck</h1>")
        for fmt in ("pptx", "pdf"):
            with self.assertRaises(helper.ExportError) as ctx:
                helper.run_export(helper.Chromium(), fmt, src, self.dir / f"page.{fmt}")
            self.assertEqual(ctx.exception.code, "NO_SLIDES")


@unittest.skipUnless(have_chromium(), "Chromium is not installed")
class WarmBrowserTest(unittest.TestCase):
    """A real warm browser: a job runs in it, and a killed job leaves no context behind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.proc = helper.launch_chromium(Path(self.tmp.name) / "profile", self.port)
        self.addCleanup(helper.stop_chromium, self.proc)
        self.cdp = helper.load_cdp_module()
        deadline = time.monotonic() + 30
        while True:
            try:
                self.ws = self.cdp.discover_ws_url(self.port)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def contexts(self):
        client = self.cdp.CdpClient(self.ws)
        client.connect()
        try:
            return client.call("Target.getBrowserContexts", {})["browserContextIds"]
        finally:
            client.close()

    def test_a_lint_runs_in_the_warm_browser(self):
        with patch.object(helper, "WARM_PORT", self.port):
            result = helper.run_export(helper.Chromium(), "lint", FIXTURE, None)
        self.assertTrue(result["ok"] and result["warm"])
        self.assertEqual(result["slides"], 3)
        self.assertEqual(self.contexts(), [])

    def test_a_job_killed_mid_way_leaves_no_context_behind(self):
        script = (
            "import importlib.machinery as m, importlib.util as u, sys, time\n"
            f"l = m.SourceFileLoader('h', {str(HERE.parent / 'bin' / 'superchat-deck-export')!r})\n"
            "h = u.module_from_spec(u.spec_from_loader('h', l)); l.exec_module(h)\n"
            f"h.WARM_PORT = {self.port}\n"
            "b = h.Chromium().__enter__(); print('attached', b.warm, flush=True); time.sleep(60)\n"
        )
        job = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(job.kill)
        self.assertEqual(job.stdout.readline().strip(), "attached True")
        self.assertEqual(len(self.contexts()), 1)
        job.send_signal(signal.SIGKILL)
        job.wait()
        deadline = time.monotonic() + 10
        while self.contexts() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.contexts(), [])


class GoogleFontStylesheetTest(unittest.TestCase):
    """Ported from open-design pptx-editable-fidelity.test.ts (fetchGoogleFontStylesheets)."""

    def test_asks_google_fonts_for_ttf_compatible_css_and_skips_other_hosts(self):
        calls = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b"@font-face { src: url('font.ttf'); }"

        def fake_urlopen(request, timeout):
            calls.append((request.full_url, request.get_header("User-agent"), timeout))
            return Resp()

        with patch.object(helper.urllib.request, "urlopen", fake_urlopen):
            sheets = helper.fetch_google_font_stylesheets(
                ["https://fonts.googleapis.com/css2?family=Fraunces", "https://example.test/fonts.css"]
            )
        self.assertEqual(
            calls, [("https://fonts.googleapis.com/css2?family=Fraunces", "Mozilla/5.0", 10.0)]
        )
        self.assertEqual(
            sheets,
            [
                {
                    "cssText": "@font-face { src: url('font.ttf'); }",
                    "url": "https://fonts.googleapis.com/css2?family=Fraunces",
                }
            ],
        )

    def test_a_failing_stylesheet_request_never_fails_the_export(self):
        def boom(*_args, **_kwargs):
            raise TimeoutError("never answers")

        with patch.object(helper.urllib.request, "urlopen", boom):
            self.assertEqual(
                helper.fetch_google_font_stylesheets(["https://fonts.googleapis.com/css2?family=X"]),
                [],
            )


if __name__ == "__main__":
    unittest.main()
