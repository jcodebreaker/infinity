import type { LatestBrief } from "@superchat/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import { rpc } from "../../lib/rpc";

/** The calls the card makes; the live RPC unless a test or preview supplies its own. */
export type BriefWire = {
  latest: (input: { botId: string }) => Promise<LatestBrief>;
  refresh: (input: { botId: string; taskId: string }) => Promise<{ ok: true }>;
};

const LIVE_WIRE: BriefWire = {
  latest: (input) => rpc.brief.latest(input),
  refresh: (input) => rpc.brief.refresh(input),
};

/** While a refresh runs, look for the new version this often, and give up after this long. */
const REFRESH_POLL_MS = 5_000;
const REFRESH_LIMIT_MS = 4 * 60_000;

/** The person's newest morning brief, re-read on focus and on `reload` (Home shown again), and
 * Refresh now (a new version, made in the background). */
export function useLatestBrief(botId: string, wire: BriefWire = LIVE_WIRE) {
  const [brief, setBrief] = useState<LatestBrief | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [failed, setFailed] = useState(false);
  const since = useRef<string | null>(null);

  const load = useCallback(async () => {
    // No Muse yet: nothing to ask for.
    if (!botId) return;
    try {
      const next = await wire.latest({ botId });
      setBrief(next);
      if (since.current !== null && next.page && next.page.savedAt !== since.current) {
        since.current = null;
        setRefreshing(false);
      }
    } catch {
      // The card is a shortcut: without an answer it stays as it was (or hidden).
    }
  }, [botId, wire]);

  useEffect(() => {
    void load();
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  useEffect(() => {
    if (!refreshing) return;
    const poll = window.setInterval(() => void load(), REFRESH_POLL_MS);
    const stop = window.setTimeout(() => {
      since.current = null;
      setRefreshing(false);
    }, REFRESH_LIMIT_MS);
    return () => {
      window.clearInterval(poll);
      window.clearTimeout(stop);
    };
  }, [refreshing, load]);

  const refresh = useCallback(async () => {
    const taskId = brief?.taskId;
    if (!taskId || refreshing) return;
    setFailed(false);
    since.current = brief?.page?.savedAt ?? "";
    setRefreshing(true);
    try {
      await wire.refresh({ botId, taskId });
    } catch {
      since.current = null;
      setRefreshing(false);
      setFailed(true);
    }
  }, [botId, brief, refreshing, wire]);

  return { brief, refreshing, failed, refresh, reload: load };
}

/** What `useLatestBrief` gives its card. */
export type LatestBriefState = ReturnType<typeof useLatestBrief>;
