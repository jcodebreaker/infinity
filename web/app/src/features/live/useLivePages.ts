import type { LivePage } from "@superchat/contracts";
import { useEffect, useSyncExternalStore } from "react";
import { rpc } from "../../lib/rpc";

// One list of the person's live pages for every Library card and filter, read in one engine call
// (not one per card) and shared; a mount re-reads it at most every few seconds.
const FRESH_MS = 5_000;

let pages: LivePage[] | null = null;
let fetchedAt = 0;
let inflight: Promise<void> | null = null;
const listeners = new Set<() => void>();

const emit = () => {
  for (const listener of listeners) listener();
};

/** Re-reads the list unless one read is under way or it is fresh (`force` skips the freshness). */
export function refreshLivePages(force = false): Promise<void> {
  if (inflight) return inflight;
  if (!force && pages && Date.now() - fetchedAt < FRESH_MS) return Promise.resolve();
  inflight = rpc.live
    .pages({})
    .then((next) => {
      pages = next;
      fetchedAt = Date.now();
      emit();
    })
    .catch(() => {
      // The badge is a nicety: without the list the cards show no tag.
      pages ??= [];
      emit();
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

/** The person's live pages; `null` until the first read. */
export function useLivePages(): LivePage[] | null {
  useEffect(() => {
    void refreshLivePages();
  }, []);
  return useSyncExternalStore(subscribe, () => pages);
}

/** How a live page is doing, for its Library tag. */
export type LivePageHealth = "ok" | "failed" | "paused";

export function livePageHealth(page: LivePage): LivePageHealth {
  if (page.paused) return "paused";
  return page.failures > 0 && page.lastFailure ? "failed" : "ok";
}

/** Test seam: forget the shared list. */
export function resetLivePages() {
  pages = null;
  fetchedAt = 0;
  inflight = null;
}
