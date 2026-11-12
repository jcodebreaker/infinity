# A deck is an HTML file, exported in the host

People ask the Muse for slide decks and want them in PowerPoint they can keep editing. We could have the Muse write a .pptx directly, or export slide screenshots. Instead a Deck is a single self-contained HTML file named `*.deck.html` (1920x1080 slides on a fixed framework), and the PowerPoint and PDF are produced from that page in the person's host.

- **One source.** The Muse writes only slides. `deck_new` assembles the file around them (scale-to-fit, navigation, print rules, a template's theme, embedded fonts), so every deck starts exact for export. Every text or media block carries a stable `data-superchat-id`, which is also what editing will address next.
- **Export in the host.** `deck_export` (and `POST /v1/decks/{id}/export`, which wakes the host first) runs `superchat-deck-export` in a separate headless Chromium over CDP. PowerPoint comes from the vendored dom-to-pptx bundle plus Open Design's ported capture logic: native text, shapes and fonts. PDF comes from the same render through `Page.printToPDF`, which stays vector. The result is saved as a new file in the same chat.
- **Never screenshots.** Nothing in either file is a picture of a slide. The one raster is Open Design's layered background: a gradient, filter or blend that PowerPoint cannot express is painted by Chromium into a transparent picture behind native, editable text. The Muse's deck rules steer away from those effects, and `deck_check` names the ones that remain.
- **Exact by construction.** `deck_check` measures the laid-out deck in the same render the export reads (content past the slide or inside the footer band, clipped text, missing ids, unembedded fonts), so a fault is fixed in the HTML instead of being papered over at export.

## Considered options

- **The Muse writes a .pptx (python-pptx).** Rejected: every slide is hand-placed, the page and the file drift, and the person has two sources.
- **Per-slide screenshots in a .pptx.** Rejected: not editable, blurry on a large screen.
- **Export in Electron or the server.** Rejected: the Muse's host already has Chromium and the files; the server stays free of a browser.

## Consequences

- The host image carries `superchat-deck-export`, the page-side capture module and the vendored bundle; a change to them needs an image rebuild.
- A deck is recognised by its file name (`.deck.html`), so the panel and the cards know without opening the file.
- The viewer and the export share the framework's navigation protocol (`superchat:slide`, `superchat:slide-state`, `superchat:deck-ready`).

## Amendments

**Amended 2026-10-19 (fix wave).**

- **Every save of a deck is checked.** `artifact_save` and `artifact_patch` of a `*.deck.html` run `deck_check`'s own check on the saved bytes and return it as `layout` (`{"ok", "errors"?, "warnings"?}`, or `{"checked": false, "why"}` when the render could not run), so a reply reports fit from the save, not from a separate call the model may skip (`artifacts/handlers.py`, `decks/authoring.py` `saved_layout`).
- **Links are allowed in slides.** A link's `<a href>` loads nothing, so it no longer gets a deck refused; any other web reference (a `src`, `url()`, an SVG `href`, a web font, `@import`) still does, and the refusal quotes the reference (`decks/kit.py`).
- **The kit step is explicit.** `deck_new` with `path` and `title` only is the kit: it writes nothing and returns the layouts, the chart recipes and the theme, and says the next call carries all the slides. The tool description states the no-web-assets and no-script rules up front; Helpers, whose prompt lists the layouts, skip the kit call (`decks/tools.py`).
