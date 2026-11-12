// Portions modified from nexu-io/open-design apps/web/src/edit-mode/source-patches.ts@802708f, Apache-2.0; changes: style/translate helpers only; patch application is in the engine.
import type { DeckEditTarget, HtmlPatch } from "@superchat/contracts";

// Pure helpers for the deck editor: colour and length reading, translate handling for the
// position nudge, how style changes become patches, and how neighbouring patches coalesce.

export type StyleChanges = Record<string, string | null>;

/** `rgb(10, 20, 30)` / `rgba(...)` -> `#0a141e`; "" for transparent or unreadable values. */
export function toHex(value: string | undefined): string {
  if (!value) return "";
  if (/^#[0-9a-f]{6}$/i.test(value)) return value.toLowerCase();
  const m = /^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+%?))?\s*\)$/i.exec(value);
  if (!m) return "";
  const alpha =
    m[4] === undefined ? 1 : m[4].endsWith("%") ? Number.parseFloat(m[4]) / 100 : Number(m[4]);
  if (alpha === 0) return "";
  return `#${[m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, "0")).join("")}`;
}

/** A CSS length in px (`64px`, `64`) as a number, or null (`normal`, `auto`, ``). */
export function px(value: string | undefined): number | null {
  if (!value) return null;
  const m = /^(-?\d+(?:\.\d+)?)(?:px)?$/.exec(value.trim());
  return m ? Number(m[1]) : null;
}

/** Rounds to at most 2 decimals and drops a trailing `.0`. */
export const num = (n: number): string => String(Math.round(n * 100) / 100);

export type Translate = { x: number; y: number };

const TRANSLATE = /translate\(\s*(-?[\d.]+)(?:px)?\s*(?:,\s*(-?[\d.]+)(?:px)?)?\s*\)/;

/** The translate() the editor wrote (or the element authored) in an inline transform. */
export function readTranslate(transform: string | undefined): Translate {
  const m = transform ? TRANSLATE.exec(transform) : null;
  return { x: m ? Number(m[1]) : 0, y: m?.[2] ? Number(m[2]) : 0 };
}

/** The inline transform with its translate() replaced (other functions are kept); `null` clears
 * the property when nothing else is left. */
export function withTranslate(transform: string | undefined, to: Translate): string | null {
  const rest = (transform ?? "").replace(TRANSLATE, "").replace(/\s+/g, " ").trim();
  const moved = to.x === 0 && to.y === 0 ? "" : `translate(${num(to.x)}px, ${num(to.y)}px)`;
  const joined = [moved, rest].filter(Boolean).join(" ");
  return joined || null;
}

/** The text of a font token's first family, for matching a computed `font-family`. */
export function firstFamily(value: string | undefined): string {
  return (value ?? "").split(",")[0]?.replace(/^["'\s]+|["'\s]+$/g, "") ?? "";
}

/** An id the bridge made from an element's place in a page because it has no
 * `data-superchat-id`: good for selecting and asking, never for a hand edit (the engine refuses it,
 * since a place is not a stable address). */
export const isPathId = (id: string): boolean => id.startsWith("~");

/** Human label of a target for the handle and the panel: `Slide 2 · Heading` in a deck,
 * `Heading` in a page (`inDeck` false). */
export function targetLabel(
  target: DeckEditTarget,
  words: Record<string, string>,
  inDeck = true,
): string {
  if (target.kind === "slide") return `${words.slide ?? "Slide"} ${target.slide}`;
  const tag = /^h[1-6]$/.test(target.tag)
    ? (words.heading ?? "Heading")
    : target.kind === "image"
      ? (words.image ?? "Image")
      : target.kind === "link"
        ? (words.link ?? "Link")
        : !inDeck
          ? target.tag // a page's elements are many kinds of thing: its tag says which
          : target.kind === "text"
            ? (words.text ?? "Text")
            : (words.box ?? "Box");
  return inDeck ? `${words.slide ?? "Slide"} ${target.slide} · ${tag}` : tag;
}

/** What a target showed for the changed properties, for the engine's "you changed X" wording. */
function shownBefore(target: DeckEditTarget | undefined, props: string[]): Record<string, string> {
  const before: Record<string, string> = {};
  for (const prop of props) {
    if (prop === "transform") continue;
    const value = target?.inline[prop] ?? target?.computed[prop];
    if (value) before[prop] = value.slice(0, 200);
  }
  return before;
}

/** One `set-style` patch per target. */
export function stylePatches(
  targets: readonly DeckEditTarget[],
  changes: StyleChanges,
): HtmlPatch[] {
  return targets.map((target) => ({
    kind: "set-style" as const,
    id: target.id,
    style: { ...changes },
    before: shownBefore(target, Object.keys(changes)),
  }));
}

/** Merges neighbouring `set-style` patches on one id (later wins) so a slider drag is one patch;
 * every other kind keeps its order. */
export function coalescePatches(patches: readonly HtmlPatch[]): HtmlPatch[] {
  const out: HtmlPatch[] = [];
  for (const patch of patches) {
    const prev = out[out.length - 1];
    if (patch.kind === "set-style" && prev?.kind === "set-style" && prev.id === patch.id) {
      out[out.length - 1] = {
        ...prev,
        style: { ...prev.style, ...patch.style },
        before: { ...patch.before, ...prev.before },
      };
    } else if (patch.kind === "set-full-source") {
      out.length = 0;
      out.push(patch);
    } else {
      out.push(patch);
    }
  }
  return out;
}

/** The preview a patch implies in the live frame (style patches only). */
export function previewOf(patch: HtmlPatch): { id: string; style: StyleChanges } | null {
  return patch.kind === "set-style" ? { id: patch.id, style: patch.style } : null;
}

/** Why the engine refused a hand edit (`artifacts.edit`): the file moved on since the editor
 * opened it ("stale"), or a patch it will not apply exactly ("inexact": ask superchat instead). */
export function editConflict(error: unknown): "stale" | "inexact" | null {
  const failure = error as { code?: unknown; data?: { code?: unknown } } | null;
  if (failure?.code !== "CONFLICT") return null;
  return failure.data?.code === "artifact_stale" ? "stale" : "inexact";
}
