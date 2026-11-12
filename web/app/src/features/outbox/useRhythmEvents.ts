import { type RefObject, useEffect } from "react";
import { rpc } from "../../lib/rpc";

type RhythmEvent = "shown" | "opened" | "acted_on";

/** How long a message stays on screen, in a visible tab, before it counts as opened. */
const OPENED_AFTER_MS = 3000;

/** What this page already reported: each event once per message (the engine keeps the first). */
const reported = new Set<string>();

export function reportRhythm(botId: string, itemId: string, event: RhythmEvent): void {
  const key = `${itemId}:${event}`;
  if (!botId || reported.has(key)) return;
  reported.add(key);
  rpc.rhythm.report({ botId, itemId, event }).catch(() => reported.delete(key));
}

/**
 * Tell the engine's rhythm (ADR 0038) what the person did with a proactive message, without
 * anything visible: `shown` once it is on screen in a visible tab, `opened` once it stayed there
 * a few seconds, `acted_on` when a link or button inside the message (its file, its draft) is
 * used. *Open topic* is reported by the pick itself; *Not useful* is a mute and never reported.
 *
 * @param anchor An element under the message; its bubble frame is the message watched.
 */
export function useRhythmEvents(
  botId: string,
  itemId: string,
  anchor: RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    const root = anchor.current;
    const frame = root?.closest<HTMLElement>('[data-testid="message-bubble-frame"]') ?? root;
    if (!botId || !root || !frame) return;
    let inView = false;
    let timer: number | undefined;
    const update = () => {
      window.clearTimeout(timer);
      if (!inView || document.visibilityState !== "visible") return;
      reportRhythm(botId, itemId, "shown");
      timer = window.setTimeout(() => reportRhythm(botId, itemId, "opened"), OPENED_AFTER_MS);
    };
    const observer =
      typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver(
            (entries) => {
              inView = entries.some((entry) => entry.isIntersecting);
              update();
            },
            { threshold: 0.5 },
          );
    observer?.observe(frame);
    document.addEventListener("visibilitychange", update);
    const onClick = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      // The label's own actions report themselves (or, for Not useful, never).
      if (!target || root.contains(target)) return;
      if (target.closest("a, button, [role='button']")) reportRhythm(botId, itemId, "acted_on");
    };
    frame.addEventListener("click", onClick, true);
    return () => {
      window.clearTimeout(timer);
      observer?.disconnect();
      document.removeEventListener("visibilitychange", update);
      frame.removeEventListener("click", onClick, true);
    };
  }, [botId, itemId, anchor]);
}
