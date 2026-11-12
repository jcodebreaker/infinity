import { useEffect, useState } from "react";

/** The current time in ms, ticking every second while `active` (for live elapsed times);
 * frozen otherwise, so settled rows cost nothing. */
export function useNow(active: boolean, intervalMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [active, intervalMs]);
  return now;
}
