import { useEffect, useRef } from "react";

/**
 * Runs `tick` every `intervalMs` while the tab is visible, and once more when the tab becomes
 * visible or the window regains focus. Nothing runs while hidden or when `enabled` is false.
 * Overlapping runs are skipped, so a slow request never stacks up.
 */
export function useVisiblePoll(tick: () => unknown, intervalMs: number, enabled = true) {
  const latest = useRef(tick);
  latest.current = tick;
  useEffect(() => {
    if (!enabled) return;
    let busy = false;
    const run = () => {
      if (busy || document.visibilityState === "hidden") return;
      busy = true;
      void Promise.resolve(latest.current())
        .catch(() => undefined)
        .finally(() => {
          busy = false;
        });
    };
    const timer = window.setInterval(run, intervalMs);
    window.addEventListener("focus", run);
    document.addEventListener("visibilitychange", run);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", run);
      document.removeEventListener("visibilitychange", run);
    };
  }, [intervalMs, enabled]);
}
