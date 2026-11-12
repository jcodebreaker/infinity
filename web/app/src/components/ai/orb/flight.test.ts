// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";
import { flyOrb } from "./flight";

/** The document, loosely typed so a test can add or remove `startViewTransition`. */
const vtDocument = document as unknown as { startViewTransition?: unknown };

function orbAt(rect: { left: number; top: number; size: number }) {
  const element = document.createElement("span");
  element.setAttribute("data-superchat-orb", "");
  element.getBoundingClientRect = () =>
    ({
      left: rect.left,
      top: rect.top,
      width: rect.size,
      height: rect.size,
    }) as DOMRect;
  return element;
}

function setReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn().mockReturnValue({ matches: reduce }) as never;
}

afterEach(() => {
  delete vtDocument.startViewTransition;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("flyOrb", () => {
  it("runs the update inside a View Transition when the browser has one", () => {
    setReducedMotion(false);
    const startViewTransition = vi.fn((update: () => void) => update());
    vtDocument.startViewTransition = startViewTransition;
    const update = vi.fn();
    expect(flyOrb(update)).toBe("view-transition");
    expect(startViewTransition).toHaveBeenCalledTimes(1);
    expect(update).toHaveBeenCalledTimes(1);
  });

  it("falls back to a FLIP from the hero to the orb's new home", () => {
    setReducedMotion(false);
    const hero = orbAt({ left: 400, top: 300, size: 96 });
    document.body.append(hero);
    const home = orbAt({ left: 20, top: 20, size: 34 });
    const animate = vi.fn();
    home.animate = animate as never;
    const flight = flyOrb(() => {
      hero.remove();
      document.body.append(home);
    });
    expect(flight).toBe("flip");
    expect(animate).toHaveBeenCalledTimes(1);
    const [keyframes, options] = animate.mock.calls[0] as [Keyframe[], KeyframeAnimationOptions];
    expect(keyframes[0]?.transform).toContain("scale(");
    expect(keyframes[1]?.transform).toBe("translate(0, 0) scale(1)");
    expect(options.duration).toBe(600);
  });

  it("cuts instantly under reduced motion, even with View Transitions", () => {
    setReducedMotion(true);
    const startViewTransition = vi.fn();
    vtDocument.startViewTransition = startViewTransition;
    const update = vi.fn();
    expect(flyOrb(update)).toBe("cut");
    expect(update).toHaveBeenCalledTimes(1);
    expect(startViewTransition).not.toHaveBeenCalled();
  });
});
