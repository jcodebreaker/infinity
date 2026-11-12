import type { OutboxUnread } from "@superchat/contracts";
import { useEffect, useSyncExternalStore } from "react";
import { watchFamily } from "../../lib/family-stream";
import { rpc } from "../../lib/rpc";

const NONE: OutboxUnread = { count: 0, firstItemId: null, lastSeenItemId: null };

// One unread state per Muse, shared by every surface that shows it (dock dot, tab title,
// divider), so they never disagree and read it with one request.
const states = new Map<string, OutboxUnread>();
const listeners = new Map<string, Set<() => void>>();
const inFlight = new Map<string, Promise<void>>();

/** Set the Muse's unread state (from a read, or from what `seen` answered) and tell its readers. */
export function setOutboxUnread(botId: string, next: OutboxUnread): void {
  states.set(botId, next);
  for (const listener of listeners.get(botId) ?? []) listener();
}

/** Read the Muse's unread proactive messages again (one request at a time). */
export function refreshOutboxUnread(botId: string): Promise<void> {
  const pending = inFlight.get(botId);
  if (pending) return pending;
  const request = rpc.outbox
    .unread({ botId })
    .then((next) => setOutboxUnread(botId, next))
    // Keep the last known state on a transient failure; the next change reads again.
    .catch(() => undefined)
    .finally(() => inFlight.delete(botId));
  inFlight.set(botId, request);
  return request;
}

/**
 * The proactive messages the person has not seen (ADR 0036). Read on mount, whenever a reply
 * is stored or a turn ends in the Conversation (the family stream), on a reconnect, and when
 * the tab comes back.
 */
export function useOutboxUnread(botId: string | null | undefined): OutboxUnread {
  const state = useSyncExternalStore(
    (onChange) => {
      if (!botId) return () => undefined;
      const set = listeners.get(botId) ?? new Set();
      set.add(onChange);
      listeners.set(botId, set);
      return () => set.delete(onChange);
    },
    () => (botId ? (states.get(botId) ?? NONE) : NONE),
  );
  useEffect(() => {
    if (!botId) return;
    void refreshOutboxUnread(botId);
    const stopFamily = watchFamily(botId, (event) => {
      if (event.type === "messageDone" || event.type === "turnDone" || event.type === "open") {
        void refreshOutboxUnread(botId);
      }
    });
    const onVisible = () => {
      if (document.visibilityState === "visible") void refreshOutboxUnread(botId);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      stopFamily();
    };
  }, [botId]);
  return state;
}
