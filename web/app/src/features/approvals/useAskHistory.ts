import type { AnsweredAsk } from "@superchat/contracts";
import { useEffect, useState } from "react";
import { rpc } from "../../lib/rpc";

/**
 * The asks answered in the last 30 days, newest first, read while `enabled` (the History tab is
 * showing) and again whenever the open asks change (`asksKey`), since an answer moves one here.
 */
export function useAskHistory(
  botId: string,
  enabled: boolean,
  asksKey: string,
): { history: AnsweredAsk[] | null; failed: boolean } {
  const [history, setHistory] = useState<AnsweredAsk[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!enabled || !botId) return;
    let live = true;
    rpc.asks.history({ botId }).then(
      (rows) => {
        if (!live) return;
        setHistory(rows);
        setFailed(false);
      },
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [botId, enabled, asksKey]);
  return { history, failed };
}
