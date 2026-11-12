import * as z from "zod";

// The postMessage protocol between the artifact editor (host) and the edit bridge injected into a
// sandboxed HTML frame (a deck, or any page). Every message carries `v` and the per-frame `nonce`;
// the host also checks `event.source`. The bridge talks to `window.parent` only, and only the
// `superchat:` types cross the sandbox shell's relay.

export const DECK_EDIT_PROTOCOL_VERSION = 1;

const Base = { v: z.literal(DECK_EDIT_PROTOCOL_VERSION), nonce: z.string().min(8).max(128) };
const Str = (max: number) => z.string().max(max);
const StyleMap = z.record(Str(64), Str(512));
const Rect = z.object({ x: z.number(), y: z.number(), w: z.number(), h: z.number() });

export const DeckEditTargetSchema = z.object({
  id: Str(128),
  /** Element kind the editor can act on. */
  kind: z.enum(["text", "link", "image", "box", "slide"]),
  tag: Str(32),
  /** 1-based slide number the element sits on. */
  slide: z.number().int().min(1),
  /** The element's own text when it is a text leaf, else null. */
  text: Str(4000).nullable(),
  /** Inline text edit is possible (a text leaf). */
  editable: z.boolean(),
  /** Position and size in slide pixels (the 1920 x 1080 stage). */
  rect: Rect,
  /** Declarations authored inline on the element (kebab-case). */
  inline: StyleMap,
  /** Computed values the panel shows. */
  computed: StyleMap,
  href: Str(2048).nullable(),
  alt: Str(1024).nullable(),
  /** A page element's place (`main > form > button`), for asking about one with no id. */
  where: Str(300).optional(),
});
export type DeckEditTarget = z.infer<typeof DeckEditTargetSchema>;

export const DeckEditThemeSchema = z.object({
  /** Colour tokens declared on :root (name -> resolved value). */
  colors: z.array(z.object({ name: Str(64), value: Str(64) })).max(64),
  /** Font tokens (`--font-*`): the token name, the family list and the first family's name. */
  fonts: z.array(z.object({ name: Str(64), value: Str(512), label: Str(128) })).max(16),
  slideCount: z.number().int().min(0),
});
export type DeckEditTheme = z.infer<typeof DeckEditThemeSchema>;

/** Messages the bridge sends to the host. */
export const DeckEditFromFrameSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("superchat:edit-ready"), ...Base, theme: DeckEditThemeSchema }),
  z.object({
    type: z.literal("superchat:edit-selection"),
    ...Base,
    targets: z.array(DeckEditTargetSchema).max(200),
  }),
  z.object({
    type: z.literal("superchat:edit-text-session"),
    ...Base,
    id: Str(128),
    active: z.boolean(),
  }),
  z.object({
    type: z.literal("superchat:edit-text-commit"),
    ...Base,
    id: Str(128),
    text: Str(20_000),
    before: Str(20_000),
  }),
  z.object({
    type: z.literal("superchat:edit-key"),
    ...Base,
    action: z.enum(["undo", "redo", "delete", "duplicate", "nudge"]),
    dx: z.number().int().min(-100).max(100).optional(),
    dy: z.number().int().min(-100).max(100).optional(),
  }),
]);
export type DeckEditFromFrame = z.infer<typeof DeckEditFromFrameSchema>;

/** Messages the host sends to the bridge. */
export const DeckEditToFrameSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("superchat:edit-select"), ...Base, ids: z.array(Str(128)).max(200) }),
  z.object({
    type: z.literal("superchat:edit-preview"),
    ...Base,
    id: Str(128),
    style: z.record(Str(64), Str(512).nullable()),
  }),
  z.object({ type: z.literal("superchat:edit-text-finish"), ...Base, commit: z.boolean() }),
]);
export type DeckEditToFrame = z.infer<typeof DeckEditToFrameSchema>;
