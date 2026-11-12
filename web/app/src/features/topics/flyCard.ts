/** How long the card's flight takes, and when the Conversation takes over under it (ms). */
const FLIGHT_MS = 720;
export const FLIGHT_HANDOFF_MS = 330;
/** The width the thread's column has, which the card lands on. */
const THREAD_WIDTH = 760;

/**
 * Flies a picked Home topic card into the Conversation (a FLIP with Web Animations): a copy of
 * the card leaves its place and settles where the thread's column will be, then fades as the
 * pick-up card takes over. `null` under reduced motion or without Web Animations; cancelling the
 * returned animation (a pick that failed) puts the card back.
 */
export function flyCard(from: HTMLElement, into: DOMRect): Animation | null {
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return null;
  if (typeof from.animate !== "function") return null;
  const start = from.getBoundingClientRect();
  const copy = from.cloneNode(true) as HTMLElement;
  for (const attribute of ["data-peak", "data-peak-heat", "data-testid", "id"]) {
    copy.removeAttribute(attribute);
  }
  copy.setAttribute("aria-hidden", "true");
  copy.inert = true;
  Object.assign(copy.style, {
    position: "fixed",
    left: `${start.left}px`,
    top: `${start.top}px`,
    width: `${start.width}px`,
    height: `${start.height}px`,
    margin: "0",
    zIndex: "60",
    pointerEvents: "none",
    overflow: "hidden",
    animation: "none",
    transform: "none",
  });
  document.body.append(copy);
  from.style.visibility = "hidden";
  const width = Math.min(THREAD_WIDTH, into.width);
  const left = into.left + (into.width - width) / 2;
  const top = into.top + 120;
  const flight = copy.animate(
    [
      {
        left: `${start.left}px`,
        top: `${start.top}px`,
        width: `${start.width}px`,
        height: `${start.height}px`,
        borderRadius: "18px",
        opacity: 1,
      },
      {
        left: `${left}px`,
        top: `${top}px`,
        width: `${width}px`,
        height: "120px",
        borderRadius: "22px",
        opacity: 1,
        offset: 0.8,
      },
      {
        left: `${left}px`,
        top: `${top}px`,
        width: `${width}px`,
        height: "120px",
        borderRadius: "22px",
        opacity: 0,
      },
    ],
    { duration: FLIGHT_MS, easing: "cubic-bezier(.6,0,.2,1)" },
  );
  const land = () => {
    copy.remove();
    from.style.visibility = "";
  };
  flight.onfinish = land;
  flight.oncancel = land;
  return flight;
}
