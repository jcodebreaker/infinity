import { useSyncExternalStore } from "react";

// "Show details" from the chat: open the Activity panel and bring one Activity's row into view.
// One request at a time; the panel that shows the row settles it, so a later mount does not
// scroll again.

export interface ActivityFocus {
  /** The Activity to bring into view; null opens the panel without a row to find. */
  activityId: string | null;
  nonce: number;
}

let current: ActivityFocus | null = null;
let nonce = 0;
const listeners = new Set<() => void>();

function notify() {
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Ask the Activity panel to open on this Activity. */
export function focusActivity(activityId: string | null): void {
  nonce += 1;
  current = { activityId, nonce };
  notify();
}

/** The panel has handled this request. */
export function settleActivityFocus(handled: number): void {
  if (current?.nonce !== handled) return;
  current = null;
  notify();
}

/** The pending request, if any. */
export function useActivityFocus(): ActivityFocus | null {
  return useSyncExternalStore(subscribe, () => current);
}
