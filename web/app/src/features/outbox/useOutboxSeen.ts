import type { ThreadMessage } from "@superchat/contracts";
import { useEffect, useRef } from "react";
import { rpc } from "../../lib/rpc";
import { setOutboxUnread, useOutboxUnread } from "./useOutboxUnread";

/**
 * Tell the engine the person saw the Conversation up to its newest proactive message: only
 * while the chat is on screen (the tab visible, nothing over it) and scrolled to its end.
 */
export function useOutboxSeen({
  botId,
  messages,
  atEnd,
  shown,
}: {
  botId: string;
  messages: readonly ThreadMessage[];
  atEnd: boolean;
  /** The chat is in front: no sheet or panel covers it. */
  shown: boolean;
}): void {
  const { count } = useOutboxUnread(botId);
  const reported = useRef<string | null>(null);
  useEffect(() => {
    if (!botId || count === 0 || !atEnd || !shown) return;
    const report = () => {
      if (document.visibilityState !== "visible") return;
      const newest = messages.findLast((message) => message.proactive);
      if (!newest || newest.id === reported.current) return;
      reported.current = newest.id;
      rpc.outbox
        .seen({ botId, itemId: newest.id })
        .then((next) => setOutboxUnread(botId, next))
        .catch(() => {
          reported.current = null;
        });
    };
    report();
    document.addEventListener("visibilitychange", report);
    return () => document.removeEventListener("visibilitychange", report);
  }, [botId, count, atEnd, shown, messages]);
}
