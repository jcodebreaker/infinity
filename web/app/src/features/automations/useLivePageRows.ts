import type { LivePage } from "@superchat/contracts";
import { useCallback, useEffect, useState } from "react";
import { rpc } from "../../lib/rpc";
import { useVisiblePoll } from "../../lib/use-visible-poll";

const REFRESH_MS = 10_000;

/** The person's live pages for the Automations list, re-read while the sheet is open. A failed
 * read leaves them out (`null`) rather than breaking the list of ordinary automations. */
export function useLivePageRows(enabled: boolean) {
  const [pages, setPages] = useState<LivePage[] | null>(null);
  const refresh = useCallback(async () => {
    try {
      setPages(await rpc.live.pages({}));
    } catch {
      setPages((current) => current);
    }
  }, []);
  useEffect(() => {
    if (enabled) void refresh();
  }, [enabled, refresh]);
  useVisiblePoll(refresh, REFRESH_MS, enabled);
  const resume = useCallback(
    async (artifactId: string) => {
      await rpc.live.resume({ artifactId });
      await refresh();
    },
    [refresh],
  );
  return { pages, refresh, resume };
}
