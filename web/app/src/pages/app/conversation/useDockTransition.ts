import type { RefObject } from "react";
import { useLayoutEffect, useRef } from "react";

/**
 * Glides the composer from the empty Conversation's centered start page down to its docked
 * place once the first message lands (a FLIP: the element jumps to its new layout spot, then
 * animates from where it was). Positions are only read while centered, which is the one side
 * the glide starts from, so a busy thread pays nothing. Skipped under reduced motion.
 */
export function useDockTransition(
  ref: RefObject<HTMLElement | null>,
  centered: boolean,
  /** True when something else (a View Transition) already moves the composer. */
  handledElsewhere?: () => boolean,
): void {
  const centeredTop = useRef<number | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (centered) {
      centeredTop.current = element.offsetTop;
      return;
    }
    const from = centeredTop.current;
    centeredTop.current = null;
    if (from === null || typeof element.animate !== "function") return;
    if (handledElsewhere?.()) return;
    const delta = from - element.offsetTop;
    if (Math.abs(delta) < 2) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    element.animate([{ transform: `translateY(${delta}px)` }, { transform: "translateY(0)" }], {
      duration: 420,
      easing: "cubic-bezier(0.2, 0.8, 0.2, 1)",
    });
  });
}
