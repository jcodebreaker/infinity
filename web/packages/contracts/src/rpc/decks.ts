import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";
import { ArtifactSchema } from "./artifacts.js";

/** A deck is an HTML artifact whose file name ends with this (the engine uses the same marker). */
export const DECK_NAME_SUFFIX = ".deck.html";

/** True when an artifact file name marks a deck (`*.deck.html`, any case). */
export function isDeckArtifactName(name: string): boolean {
  return name.length > DECK_NAME_SUFFIX.length && name.toLowerCase().endsWith(DECK_NAME_SUFFIX);
}

/** Comments, `<style>` and `<script>` bodies: text that mentions a slide tag without being one. */
const NOT_MARKUP = /<!--[\s\S]*?-->|<(style|script)\b[^>]*>[\s\S]*?<\/\1\s*>/gi;

/**
 * How many slides a deck's HTML holds (its `<section class="slide …">` elements). The kit's
 * framework CSS names `<section class="slide">` in a comment, so only real markup is counted.
 */
export function deckSlideCount(html: string): number {
  return html.replace(NOT_MARKUP, "").match(/<section\b[^>]*\bclass="[^"]*\bslide\b/g)?.length ?? 0;
}

export const DECK_EXPORT_FORMATS = ["pptx", "pdf"] as const;
export const DeckExportFormatSchema = z.enum(DECK_EXPORT_FORMATS);
export type DeckExportFormat = z.infer<typeof DeckExportFormatSchema>;

export const DECK_THEME_CATEGORIES = ["professional", "editorial", "bold", "dark"] as const;

/** One entry of the deck theme dictionary; `preview` is a small thumbnail as a `data:` URI. */
export const DeckThemeSchema = z.object({
  id: z.string(),
  name: z.string(),
  /** Two to four words, shown under a picker tile's thumbnail. */
  tagline: z.string(),
  mood: z.string(),
  category: z.enum(DECK_THEME_CATEGORIES),
  mode: z.enum(["light", "dark"]),
  bestFor: z.string(),
  preview: z.string(),
});
export type DeckTheme = z.infer<typeof DeckThemeSchema>;

export const decksContract = {
  decks: {
    /** The id of the theme a deck is on, or null for a hand-made deck (the picker's check mark). */
    theme: oc
      .input(z.object({ artifactId: Id }))
      .output(z.object({ theme: z.string().nullable() })),
    /** The theme dictionary behind the deck panel's Theme picker. */
    themes: oc
      .input(z.object({}))
      .output(z.object({ themes: z.array(DeckThemeSchema), defaultTheme: z.string() })),
    /** The kit's sample deck (cover, agenda, figures, chart, table, quote, close) built in one
     * theme: a complete `.deck.html` for the theme preview. */
    sample: oc
      .input(z.object({ themeId: z.string().min(1).max(60) }))
      .output(z.object({ html: z.string() })),
    /** Exports a deck to editable PowerPoint or vector PDF in the person's host; the result
     * is a new artifact in the deck's chat. */
    export: oc
      .input(z.object({ artifactId: Id, format: DeckExportFormatSchema }))
      .output(ArtifactSchema),
  },
};
