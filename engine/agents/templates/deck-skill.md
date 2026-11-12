---
name: decks
description: The deck layouts, surfaces, footer and chart rules. Load it once before you write a deck's slides.
---

The layouts every `deck_new` slide is built from. For an example slide of any layout, call
`deck_new` with `layout` set to its name (`l-points`, `l-table` ...); a chart recipe the same way.
A darker, lighter or other look for an existing deck is `deck_theme_set` (`deck_themes` gives each
theme's mode); `artifact_outline(element="theme")` reads its theme tokens and its own CSS.

{{DECK_LAYOUTS}}
