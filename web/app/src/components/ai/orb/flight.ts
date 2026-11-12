/**
 * The orb's flight from the start page to its home (the old design notes "Orb"): one object
 * moving, not two. Both the hero orb and its destination carry `view-transition-name: superchat-orb`
 * (only one is ever mounted), so the View Transitions API morphs one into the other while the
 * rest of the page crossfades. Where View Transitions are missing, a WAAPI FLIP flies the new
 * orb in from the hero's spot. Reduced motion is an instant cut.
 */

export type OrbFlight = "view-transition" | "flip" | "cut";

export const ORB_FLIGHT_MS = 600;
export const ORB_FLIGHT_EASING = "cubic-bezier(.32,.72,0,1)";

type ViewTransitionDocument = Document & {
  startViewTransition?: (update: () => void) => unknown;
};

function visibleOrb(doc: Document): HTMLElement | null {
  for (const element of doc.querySelectorAll<HTMLElement>("[data-superchat-orb]")) {
    const rect = element.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) return element;
  }
  return null;
}

/** Runs `update` (which must commit the new layout synchronously, e.g. through flushSync) and
 * moves the orb from where it was to where it lands. Returns how it moved. */
export function flyOrb(update: () => void, doc: Document = document): OrbFlight {
  const view = doc.defaultView;
  if (view?.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
    update();
    return "cut";
  }
  const vt = doc as ViewTransitionDocument;
  if (typeof vt.startViewTransition === "function") {
    vt.startViewTransition(update);
    return "view-transition";
  }
  const from = visibleOrb(doc)?.getBoundingClientRect();
  update();
  const target = visibleOrb(doc);
  if (!from || !target || typeof target.animate !== "function") return "cut";
  const to = target.getBoundingClientRect();
  if (!to.width) return "cut";
  const scale = from.width / to.width;
  const dx = from.left + from.width / 2 - (to.left + to.width / 2);
  const dy = from.top + from.height / 2 - (to.top + to.height / 2);
  target.animate(
    [
      { transform: `translate(${dx}px, ${dy}px) scale(${scale})` },
      { transform: "translate(0, 0) scale(1)" },
    ],
    { duration: ORB_FLIGHT_MS, easing: ORB_FLIGHT_EASING },
  );
  return "flip";
}
