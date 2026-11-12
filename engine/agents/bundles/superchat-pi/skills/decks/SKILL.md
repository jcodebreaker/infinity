---
name: decks
description: The deck layouts, surfaces, footer and chart rules. Load it once before you write a deck's slides.
---

The layouts every `deck_new` slide is built from. For an example slide of any layout, call
`deck_new` with `layout` set to its name (`l-points`, `l-table` ...); a chart recipe the same way.
A darker, lighter or other look for an existing deck is `deck_theme_set` (`deck_themes` gives each
theme's mode); `artifact_outline(element="theme")` reads its theme tokens and its own CSS.

Layouts (put the layout class and one surface class on each `<section class="slide ...">`):

| layout | use it for | notes |
|---|---|---|
| `l-cover` | the opening slide | kicker, `h1.title` (8 words max), `p.lead`, `p.meta` |
| `l-section` | a chapter break | `.num` (01), `h2.title` |
| `l-statement` | one sentence that matters | `.kicker`, `h2.statement` (14 words max) |
| `l-points` | up to three ideas | `.kicker`, `h2.title`, `.grid` of 3 `.point` (`.num`, `h3`, `p` of 20 words max) |
| `l-split` | two sides of one idea | `.kicker`, `h2.title`, `.cols` of 2 `.col` (`h3`, `p`, or `.panel` with `p`s) |
| `l-stats` | up to three real figures | `.kicker`, `h2.title`, `.grid` of 3 `.stat` (`.value`, `.label`); use only numbers you were given |
| `l-chart` | one Chart.js chart from the person's data | `.kicker`, `h2.title`, `.chart` holding one `<canvas id="…">`, `p.source` naming the file and columns the numbers came from, and the chart's `<script data-superchat-chart>` (recipes below). A `.cols-chart` grid puts a chart beside a `.panel` of takeaways |
| `l-table` | a small table of the person's figures | `.kicker`, `h2.title`, one `<table>` with a `<thead>` row and up to 6 `<tbody>` rows of 5 cells, each cell with its own `data-superchat-id` |
| `l-quote` | a quotation | `.kicker`, `blockquote.quote`, `.by` |
| `l-closing` | the ask or next step | `.kicker`, `h2.title`, `p.lead`, `p.contact` |

Surfaces: `t-a`, `t-b`, `t-c` (the template's three backgrounds). Alternate them for rhythm; cover and closing use `t-b`.

Every slide ends with `<div class="foot" data-superchat-id="NAME-foot"><span data-superchat-id="NAME-foot-left">Deck name</span><span data-superchat-id="NAME-foot-page">02</span></div>`.

Example slide:

```html
<section class="slide t-a l-points" data-screen-label="03 Three ideas" data-superchat-id="ideas">
  <div class="kicker" data-superchat-id="ideas-kicker">What we found</div>
  <h2 class="title" data-superchat-id="ideas-title">Three things moved</h2>
  <div class="grid" data-superchat-id="ideas-grid">
    <div class="point" data-superchat-id="ideas-a"><div class="num" data-superchat-id="ideas-a-num">01</div><h3 data-superchat-id="ideas-a-h">Short heading</h3><p data-superchat-id="ideas-a-p">One short sentence.</p></div>
    <div class="point" data-superchat-id="ideas-b"><div class="num" data-superchat-id="ideas-b-num">02</div><h3 data-superchat-id="ideas-b-h">Short heading</h3><p data-superchat-id="ideas-b-p">One short sentence.</p></div>
    <div class="point" data-superchat-id="ideas-c"><div class="num" data-superchat-id="ideas-c-num">03</div><h3 data-superchat-id="ideas-c-h">Short heading</h3><p data-superchat-id="ideas-c-p">One short sentence.</p></div>
  </div>
  <div class="foot" data-superchat-id="ideas-foot"><span data-superchat-id="ideas-foot-left">Deck name</span><span data-superchat-id="ideas-foot-page">03</span></div>
</section>
```

Charts: copy a chart recipe (`deck_new` with `layout` set to `chart-bar`, `chart-combo`, `chart-line`, `chart-pie` or `chart-radar` returns it) and change only its ids, labels, data and colors. They are Chart.js 4 on a canvas (already in the deck, offline); the export turns each one into a native PowerPoint chart the person can edit with Edit Data. Rules: the canvas stays inside a `.chart` box that has a fixed height (the CSS gives it one); every canvas id is unique in the deck; use bar (also stacked and horizontal), line, area (a filled line), pie, doughnut, radar, scatter, bubble, or a bar with a line on a second axis. `polarArea` and anything else without a PowerPoint chart type fails `deck_check`: use one of those or an `l-table` slide. Numbers come from the person's files, never from memory.
