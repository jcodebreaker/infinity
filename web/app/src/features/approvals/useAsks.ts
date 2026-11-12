import type { Ask } from "@superchat/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import { useFamilyLive, watchFamily } from "../../lib/family-stream";
import { rpc } from "../../lib/rpc";

// An approval asked or answered in any family chat arrives on the family stream, and every answer
// here refreshes at once (notifyAsksChanged), so the poll only catches the rest (a Goal's blocked
// task); focus and visibility also refresh.
const POLL_INTERVAL_MS = 30_000;
/** Several surfaces read Asks at once (badge, context panel, sheet): they share one request. */
const inFlight = new Map<string, Promise<Ask[]>>();

function listAsks(botId: string): Promise<Ask[]> {
  const pending = inFlight.get(botId);
  if (pending) return pending;
  const request = rpc.asks.list({ botId }).finally(() => inFlight.delete(botId));
  inFlight.set(botId, request);
  return request;
}
/** Fired after an Ask is answered so every surface showing that Muse's Asks refreshes at once. */
const ASKS_CHANGED_EVENT = "muse:asks-changed";

/** Tell every open Asks list (Waiting on you, the context panel, badges) to refresh. */
export function notifyAsksChanged(botId: string): void {
  window.dispatchEvent(new CustomEvent(ASKS_CHANGED_EVENT, { detail: { botId } }));
}

export type AnswerAskInput = { askId: string; runId: string; answer: string };

export type UseAsksResult = {
  /** Every open Ask, newest first. */
  asks: Ask[];
  /** Open-Ask count; derived from `asks` so the list and the badge never drift. */
  count: number;
  loading: boolean;
  answer: (input: AnswerAskInput) => Promise<void>;
};

function newestFirst(asks: Ask[]): Ask[] {
  return [...asks].sort(
    (a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime(),
  );
}

/**
 * Single source of truth for open Asks on the client (docs/glossary.md: "answering it
 * anywhere closes it everywhere"). The Waiting-on-you sheet, the Feed, and the
 * avatar's badge all read this same hook so answering in one place updates
 * every surface without a second round of state to keep in sync.
 */
export function useAsks(botId: string, options: { pollMs?: number } = {}): UseAsksResult {
  // A faster poll asked for only stands in for the family stream while it is down.
  const familyLive = useFamilyLive(botId);
  const pollMs = familyLive
    ? Math.max(options.pollMs ?? POLL_INTERVAL_MS, POLL_INTERVAL_MS)
    : (options.pollMs ?? POLL_INTERVAL_MS);
  const [asks, setAsks] = useState<Ask[]>([]);
  const [loading, setLoading] = useState(true);
  const requestId = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++requestId.current;
    // The bot id is empty until the Muse resolves; the API rejects "" with a 400.
    if (!botId) {
      setLoading(false);
      return;
    }
    try {
      const next = await listAsks(botId);
      if (id !== requestId.current) return;
      setAsks(newestFirst(next));
    } catch {
      // Keep the last known list on a transient failure; the next poll retries.
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [botId]);

  useEffect(() => {
    setLoading(true);
    void refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const onChanged = (event: Event) => {
      if ((event as CustomEvent<{ botId: string }>).detail?.botId === botId) void refresh();
    };
    window.addEventListener("focus", onVisible);
    window.addEventListener(ASKS_CHANGED_EVENT, onChanged);
    // A reconnect re-reads too: an ask may have come or gone while the stream was down.
    let connected = false;
    const stopFamily = botId
      ? watchFamily(botId, (event) => {
          if (event.type === "asksChanged") void refresh();
          else if (event.type === "open") {
            if (connected) void refresh();
            connected = true;
          }
        })
      : undefined;
    document.addEventListener("visibilitychange", onVisible);
    const poll = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, pollMs);
    return () => {
      window.removeEventListener("focus", onVisible);
      window.removeEventListener(ASKS_CHANGED_EVENT, onChanged);
      document.removeEventListener("visibilitychange", onVisible);
      window.clearInterval(poll);
      stopFamily?.();
    };
  }, [botId, refresh, pollMs]);

  const answer = useCallback(
    async (input: AnswerAskInput) => {
      let removed: Ask | undefined;
      setAsks((current) => {
        removed = current.find((ask) => ask.id === input.askId);
        return current.filter((ask) => ask.id !== input.askId);
      });
      try {
        await rpc.asks.answer(input);
        notifyAsksChanged(botId);
      } catch (err) {
        setAsks((current) =>
          removed && !current.some((ask) => ask.id === removed?.id)
            ? newestFirst([...current, removed])
            : current,
        );
        throw err;
      }
    },
    [botId],
  );

  return { asks, count: asks.length, loading, answer };
}
