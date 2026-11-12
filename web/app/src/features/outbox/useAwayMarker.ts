import { useEffect, useRef, useState } from "react";
import { useOutboxUnread } from "./useOutboxUnread";

/**
 * The message the "While you were away" divider sits above: the first one that was unread when
 * the person came back. It stays while they read (seeing it does not move it), goes once they
 * send (`followSignal` changes), and moves to the next away spell's first unread.
 */
export function useAwayMarker(botId: string, followSignal: number | undefined): string | null {
  const { firstItemId } = useOutboxUnread(botId);
  const [marker, setMarker] = useState<string | null>(null);
  const lastSignal = useRef(followSignal);
  useEffect(() => {
    if (lastSignal.current === followSignal) return;
    lastSignal.current = followSignal;
    setMarker(null);
  }, [followSignal]);
  // A new away spell (nothing was unread, now something is) puts the divider above its first.
  const lastFirst = useRef<string | null>(null);
  useEffect(() => {
    if (firstItemId && !lastFirst.current) setMarker(firstItemId);
    lastFirst.current = firstItemId;
  }, [firstItemId]);
  return marker;
}
