# Vendored: dom-to-pptx (browser UMD bundle), built from upstream source with Superchat's patch

`dom-to-pptx.bundle.js.gz` is the compressed **browser UMD build** of
[`dom-to-pptx`](https://github.com/atharva9167j/dom-to-pptx) (MIT, see `LICENSE`) 2.0.1, built by
`build.sh` from upstream source plus the patch in `patches/`. Open Design vendors the unpatched
2.0.1 bundle (nexu-io/open-design `apps/desktop/vendor/dom-to-pptx/` at commit 802708f,
Apache-2.0); building the pinned commit without Superchat's changes reproduces that file apart from
whitespace and two build-path strings.

- **Version:** 2.0.1
- **Fork point:** upstream commit `c0447adc47d3bc734d017050834edf0941adc189` (tag `v2.0.1`)
- **Global:** exposes `window.domToPptx.exportToPptx(elementOrSelector, options)`
- **Use:** `host/bin/superchat-deck-export` injects it into a headless Chromium page; the host image
  expands it with `gzip -dc` at build time (`deployment/host.Dockerfile`), and the helper
  also reads the `.gz` directly when the expanded file is absent (tests, a checkout).

It walks a rendered slide's live DOM and emits native PowerPoint shapes, text and images (not a
screenshot) via PptxGenJS. The npm package is not installed because its Node entry pulls in
puppeteer; only this self-contained browser bundle is used.

## Superchat's changes

`patches/dom-to-pptx-2.0.1-native-charts.patch` (applies to `src/` at the fork point):

1. **Native chart hook.** `prepareRenderItem` turns a `<canvas data-pptx-chart="{...}">` into an
   item of a new type `chart` (never the canvas PNG the stock code makes); `processSlide` adds it
   with PptxGenJS `slide.addChart` at the canvas box, named `__z_N__dom_N__type_chart` so
   `sortSpTree` keeps the z-order (`pptx-normalizer.js` names it "Chart N"). The attribute holds
   a PptxGenJS spec, `{ "types": [{ "type", "data", "options" }, ...], "options": {...} }`: one
   entry is a single chart, several are a combo chart. Lengths that are CSS pixels (`*FontSize`,
   `lineSize`, `lineDataSymbolSize`, a grid line's `size`) are converted to points at the slide
   scale by the hook. A malformed attribute or an unknown chart type throws: there is no
   picture fallback. The page-side module that reads Chart.js and writes the attribute is
   `../../deck-capture-page.js` (`chartJsToPptx`, `markNativeCharts`).
2. **Table row heights.** `extractTableData` also returns the rendered `<tr>` heights, and
   `processSlide` passes them to `addTable` as `rowH` (and the total as `h`), so PowerPoint does
   not collapse rows to their text height.

The bundled PptxGenJS 4.0.1 carries the same four fixes as the host's
(`../../../pptx/runtime/patches/pptxgenjs-4.0.1.patch`: gitbrent/PptxGenJS #1531, PR #1537,
PR #1540, and zero values written as 0 instead of an empty cell). `build.sh` applies that very
patch to `node_modules/pptxgenjs` before bundling.

## Updating or rebuilding

`./build.sh` clones the fork point, applies the patch, installs the locked dependencies, applies
the PptxGenJS patch, runs rollup and writes the `.gz` (`gzip -n`). It needs git, node, pnpm and
patch. To move to a newer dom-to-pptx, change `commit` in `build.sh`, rebase the patch, and
re-run `host/tests/test_deck_export.py`, `deck-export/*.test.ts` and
`engine/tests/superchat/decks/test_export_charts.py`. Do not edit the bundle by hand.
