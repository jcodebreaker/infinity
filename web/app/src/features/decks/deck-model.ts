// The deck format on the web side: reading a deck's slides out of its HTML, and the postMessage
// protocol the deck skeleton speaks (the host drives navigation, the deck reports its state).

export const DECK_PROTOCOL_VERSION = 1;
export const DECK_NAVIGATE_TYPE = "superchat:slide";
export const DECK_STATE_TYPE = "superchat:slide-state";
export const DECK_READY_TYPE = "superchat:deck-ready";

export type DeckNavigation =
  | { action: "go"; index: number }
  | { action: "next" | "prev" | "first" | "last" };

export type DeckSlide = { index: number; label: string; title: string };

/** The slides of a deck, read without running it. `title` is the label minus its "01" prefix. */
export function parseDeckSlides(html: string): DeckSlide[] {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const nodes = Array.from(doc.querySelectorAll("section.slide, .slide[data-screen-label]")).filter(
    (node) => !node.closest(".mini-slide, .overview, .notes-overlay, .thumb"),
  );
  return nodes.map((node, index) => {
    const label = (node.getAttribute("data-screen-label") ?? "").trim();
    const title = label.replace(/^\d+\s*[-.:]?\s*/, "").trim();
    return { index, label, title };
  });
}

/** Post a navigation command to a deck frame. */
export function postDeckNavigation(target: Window | null | undefined, nav: DeckNavigation): void {
  target?.postMessage(
    { type: DECK_NAVIGATE_TYPE, protocolVersion: DECK_PROTOCOL_VERSION, ...nav },
    "*",
  );
}

export type DeckEvent = { kind: "ready" } | { kind: "state"; active: number; count: number };

/** A deck's message as an event, or null for anything else. */
export function readDeckEvent(data: unknown): DeckEvent | null {
  if (!data || typeof data !== "object") return null;
  const message = data as Record<string, unknown>;
  if (message.protocolVersion !== DECK_PROTOCOL_VERSION) return null;
  if (message.type === DECK_READY_TYPE) return { kind: "ready" };
  if (
    message.type === DECK_STATE_TYPE &&
    typeof message.active === "number" &&
    typeof message.count === "number"
  ) {
    return { kind: "state", active: message.active, count: message.count };
  }
  return null;
}
