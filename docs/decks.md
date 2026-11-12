# Decks

How the assistant makes slide decks and how they become PowerPoint, as of 2026-10-19. The
decisions are [ADR 0011](adr/0011-a-deck-is-an-html-file-exported-in-the-host.md) (a deck is
an HTML file exported in the host), [ADR 0012](adr/0012-native-powerpoint-is-a-second-deck-path.md)
(native PowerPoint as a second path) and
[ADR 0013](adr/0013-deck-charts-are-chartjs-exported-as-native-charts.md) (Chart.js charts exported
as native charts). A deck is an artifact, so versions, the canvas editor and publishing are in
[artifacts.md](artifacts.md).

## The deck kit

A deck is one self-contained HTML file named `*.deck.html` with 1920x1080 slides. The kit in
`engine/omnigent/superchat/decks/kit/` holds everything except the slides:

| Path | What |
|---|---|
| `skeleton.html` | the fixed frame: scale to fit, navigation, print rules, the deck protocol (`superchat:slide`, `superchat:slide-state`, `superchat:deck-ready`) |
| `layouts.css`, `slides.html`, `menu.md` | the shared layout vocabulary and the menu the assistant picks from |
| `templates/` | 18 themes (tokens and look); `corporate-clean` is the default |
| `fonts/` | Latin-subset TTFs under the SIL OFL, embedded as data URIs |
| `charts/` | the chart recipes (bar, line, pie, radar, bar-and-line combo) and their bootstrap |
| `vendor/chart.js/` | Chart.js, pinned and vendored into every deck |

`decks/kit.py` assembles a deck from these; `decks/look.py` settles a new deck's theme.

## Authoring

The assistant writes only slides. Its tools (`decks/tools.py`) are:

- `deck_new` assembles the file around the slides (skeleton, theme, fonts). It builds the
  `template` the model passed; `look_from` is a record, never a gate. When no template is given
  (or `look_from` is `ask`), the engine chooses one from the topic and the person's taste and the
  result says which and why; the file card offers "Change look". The one refusal is background
  work with no template and no topic. Every text or media block gets a stable
  `data-superchat-id`. It has two uses, both stated in its description: the kit (`path` and
  `title` only) writes nothing and returns the layouts, chart recipes and theme, and says the next
  call carries all the slides; Helpers, whose prompt lists the layouts, skip it. The deck call is
  refused, nothing written, when the slides load anything from the web (a `src`, `url()`, an SVG
  `href`, a web font, `@import`; the refusal quotes it) or carry a script other than a chart's. A
  link's `<a href>` loads nothing and is allowed.
- `deck_check` measures the laid-out deck in the host's Chromium, the same render the export reads:
  content past the slide or in the footer band, clipped text, missing ids, unembedded fonts,
  charts with no PowerPoint equivalent. The assistant fixes the HTML and checks again. Every
  `artifact_save` or `artifact_patch` of a deck runs the same check on the saved bytes and
  returns it as `layout` (`{"ok": …, "errors": […]}`, or `{"checked": false, "why": …}`), so a
  file's studio reports fit from the save itself.
- `deck_themes` and `deck_theme_set` list and swap themes; `deck_theme_set` does not check the
  layout, so `deck_check` follows it.

The authoring rules (headline length, one idea per slide, only theme fonts, no absolute
positioning or effects PowerPoint cannot express, numbers only from the person's data) are in
`engine/agents/templates/decks.md`, rendered into the system prompt.

## Export to PPTX

`deck_export` (or `POST /v1/decks/{id}/export` from the panel's Export buttons, which starts the
host first) runs `host/bin/superchat-deck-export` in the host:

```sh
superchat-deck-export --format pptx|pdf --html PATH --out PATH
superchat-deck-export --format lint --html PATH
```

It drives a headless Chromium over CDP (the person's visible browser is never touched). The
host keeps one warm (`superchat-deck-export --serve`, started by the host's start script, which
restarts it when it exits); each job attaches to it in a browser context of its own, closed when
the job ends or its connection drops. A job launches its own Chromium only when the host serves
no warm one (development, tests).
PPTX goes through the vendored dom-to-pptx bundle (`host/share/deck-export/vendor/dom-to-pptx/`,
rebuilt from upstream plus a documented patch) and the page-side capture module
(`host/share/deck-export/deck-capture-page.js`), both ported from Open Design: native text, shapes
and fonts. PDF prints the same render with `Page.printToPDF`, which stays vector. Nothing is a
screenshot of a slide; the one raster is a layered background for a gradient or blend PowerPoint
cannot express, painted behind editable text. The result is saved as a new artifact in the same
conversation.

For a real PowerPoint with native tables, or to edit a `.pptx` the person gave it, the assistant
takes the second path: MiniMax's `pptx-generator` skill (MIT), kept in
`host/share/pptx/skill/pptx-generator/` and installed in the host at
`/usr/local/share/superchat/skills/pptx-generator`. It writes the file with PptxGenJS 4.0.1 and
checks it with python-pptx and markitdown. The runtime (`host/share/pptx/runtime/`) is installed at
image build on a fixed `NODE_PATH`, because a host cannot install packages at run time, and carries
fixes PptxGenJS's release lacks. The skill is read only on turns that take this path. In the web, a
`.pptx` opens in a read-only preview drawn by `@aiden0z/pptx-renderer`.

## Charts

A chart slide uses the `l-chart` layout with one of the chart recipes: Chart.js on a `<canvas>`,
the one script a slide may carry (`<script data-superchat-chart>`). At export the page reads each
Chart.js instance as configured and writes a PptxGenJS chart spec onto its canvas; a hook in the
pinned dom-to-pptx build adds it as a native chart with an embedded workbook, so Edit Data works in
PowerPoint. Bar, line, area, pie, doughnut, radar, scatter, bubble and a bar-with-line combo are
supported. A chart with no PowerPoint equivalent fails `deck_check` and the export with a sentence
naming the slide; there is no picture fallback. Chart and table numbers are computed with pandas
from the person's files.

## Fonts

Each theme names its fonts; the kit embeds Latin-subset TTFs as data URIs, because the viewer's CSP
blocks remote fonts. The licences sit next to the files (`kit/fonts/OFL-*.txt`). At export,
dom-to-pptx names one typeface per text run, so for a run in Chinese, Japanese or Korean the
capture module promotes the first CJK-capable family already in the stack, so PowerPoint, WPS and
Keynote land on the same real font (`host/share/deck-export/pptx-cjk-typeface.test.ts`).

## The legend fixture

The preview renderer drew pie, doughnut and varyColors-bar legends in theme colours, because those
colours live on the data points. `patches/@aiden0z__pptx-renderer@1.3.0.patch`, applied through
pnpm's `patchedDependencies`, makes each legend entry carry its point's colour.
`web/app/src/features/decks/fixtures/legend-colors.pptx` is a pie, a doughnut and a bar chart
written by the same patched PptxGenJS the host carries, regenerated with `node make-legend-fixture.cjs`
in that folder (it needs `host/share/pptx/runtime` installed);
`web/app/src/features/decks/pptx-legend-colors.test.ts` renders it with the real renderer and checks
every swatch against the colours in the chart parts.
