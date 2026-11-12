---
name: decks
description: The deck layouts, surfaces, footer and chart rules. Load it once before you write a deck's slides.
---

The layouts every `deck_new` slide is built from. For an example slide of any layout, call
`deck_new` with `layout` set to its name (`l-points`, `l-table` ...); a chart recipe the same way.
A darker, lighter or other look for an existing deck is `deck_theme_set` (`deck_themes` gives each
theme's mode); `artifact_outline(element="theme")` reads its theme tokens and its own CSS.

`deck_new` refuses slides, writing nothing, when anything in them loads from the web (an http(s) or
// URL in src, url() or an href other than a link's <a href>, a web font, @import) or they carry a
<script> other than a chart's <script data-superchat-chart>: draw a logo or icon with HTML, CSS or
inline SVG, or leave it out, and use only the theme's fonts. `deck_themes` says what each theme is
for.

{{DECK_LAYOUTS}}
