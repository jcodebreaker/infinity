import type { ReplyEvent, ThreadMessage } from "@superchat/contracts";
import type { RefObject } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { TurnStatus } from "../../../features/activity";
import { useFamilyLive, watchFamily } from "../../../lib/family-stream";
import {
  mergeNewestTranscriptPage,
  optimisticMessage,
  userCount,
} from "../../../lib/museTranscript";
import { rpc } from "../../../lib/rpc";

/** What the person just sent, until the transcript has it (`base`: their message count then). */
type Pending = { text: string; base: number };

/** Events arrive in bursts (a reply, a Helper starting): read the page once per burst. */
const REFRESH_THROTTLE_MS = 150;
/** How far back a search jump may page (50 messages a page). */
const MAX_REVEAL_PAGES = 40;
/** A reply the engine never starts to write must not leave the working row up forever. */
const REPLY_WAIT_MAX_MS = 3 * 60 * 1000;
/** While a reply is expected the family stream is the signal; this reads only while that stream
 * is down (a Muse's first message opens its Conversation, which the stream could not join
 * before). */
const REPLY_POLL_MS = 5_000;

/** How soon a dropped reply stream reconnects while the turn runs, first and at most. */
const REPLY_RETRY_MIN_MS = 1_000;
const REPLY_RETRY_MAX_MS = 3_000;

/**
 * The reply as it streams, before the transcript has it. Display only: whether the turn runs is
 * `turn`'s. `text` is still being written; `settled` was stored as a message (or its turn ended)
 * and stays until a read started after that (`settledAt`, in `reads`) shows the stored copy, so
 * the swap happens in one render. `textAt` is when `text` began, so a read that started before it
 * cannot wipe it.
 */
type Live = { text: string; textAt: number; settled: string; settledAt: number };
const NO_LIVE: Live = { text: "", textAt: 0, settled: "", settledAt: 0 };

const joinText = (a: string, b: string) => (a && b ? `${a}\n\n${b}` : a || b);

/** A streamed event onto the live reply; `replace`: the first text of a (re)connect, which is
 * everything written so far. `now` is the count of reads started when the event arrived (read
 * then, not in the state updater, which may run after a later read has started). */
function applyReply(live: Live, event: ReplyEvent, replace: boolean, now: number): Live {
  if (event.kind === "textReset") {
    return live.text
      ? { text: "", textAt: now, settled: joinText(live.settled, live.text), settledAt: now }
      : live;
  }
  if (replace || !live.text) return { ...live, text: event.delta, textAt: now };
  return { ...live, text: live.text + event.delta };
}

/** The bot's text stored after the person's newest message. */
function storedReplyText(messages: readonly ThreadMessage[]): string {
  let start = messages.length;
  while (start > 0 && messages[start - 1]?.role !== "user") start -= 1;
  return messages
    .slice(start)
    .flatMap((message) => (message.role === "bot" ? message.blocks : []))
    .map((block) => (block.kind === "text" ? block.text : ""))
    .join("\n");
}

/** What a read that started at `started` leaves of the live reply. `ended`: no turn runs and
 * nothing the person sent is waiting. */
function afterRead(
  live: Live,
  started: number,
  messages: readonly ThreadMessage[] | null,
  ended: boolean,
): Live {
  const settled = started > live.settledAt ? "" : live.settled;
  const stale = started > live.textAt && Boolean(live.text);
  const stored =
    stale && (ended || (messages && storedReplyText(messages).includes(live.text.trim())));
  const text = stored ? "" : live.text;
  return settled === live.settled && text === live.text ? live : { ...live, settled, text };
}

type Loaded = {
  threadId: string;
  messages: ThreadMessage[];
  olderCursor: string | null;
  /** The Conversation was cleared: the transcript starts at the reset. */
  reset: boolean;
  /** The history from before the reset is shown (and paged with `beforeReset`). */
  earlier: boolean;
  /** The engine is writing a reply right now. */
  running: boolean;
};

/**
 * The Muse's Conversation, read from the engine's transcript (ADR 0009) and refreshed on its
 * family stream: when a reply lands (`messageDone` for this Conversation), when the person's message
 * sent mid-turn is steered in or queued (`messageDelivery`), when a Helper starts
 * (`chatsChanged`), when it is cleared (`chatReset`), and after a reconnect. `refresh()` is for
 * the caller's own
 * moments the stream has no event for (the person's send being recorded).
 *
 * It also owns what a send needs to look instant: `beginSend` shows the person's message at once
 * (dropped when the transcript has it, or on `failSend`) and holds the Conversation as running
 * until the engine has answered. The Conversation is marked read while it is in view.
 */
export function useMuseTranscript(
  botId: string | undefined,
  scrollRef: RefObject<HTMLElement | null>,
) {
  const [loaded, setLoaded] = useState<(Loaded & { botId: string }) | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  /** Sent but not yet in the transcript; shown at once, dropped when it arrives. */
  const [pending, setPending] = useState<Pending | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  /** A reply is expected: from send until the engine has answered and gone idle. */
  const [waiting, setWaiting] = useState(false);
  /** A Stop landed: the engine's `running` is not believed until its `turnDone` or the next send. */
  const [stopped, setStopped] = useState(false);
  /** The newest turn ended stopped, until the next send. */
  const [cancelled, setCancelled] = useState(false);
  const loadedRef = useRef<(Loaded & { botId: string }) | null>(null);
  const generation = useRef(0);
  const throttle = useRef<number | undefined>(undefined);
  const firstRead = useRef<Promise<void>>(Promise.resolve());
  const [live, setLive] = useState<Live>(NO_LIVE);
  /** Reads of the newest page started so far: the clock `Live` is kept against. */
  const reads = useRef(0);

  const commit = useCallback((next: (Loaded & { botId: string }) | null) => {
    loadedRef.current = next;
    setLoaded(next);
  }, []);

  const read = useCallback(async () => {
    if (!botId) return;
    const current = generation.current;
    reads.current += 1;
    const started = reads.current;
    try {
      const page = await rpc.chats.transcript({ botId });
      if (current !== generation.current) return;
      const prev = loadedRef.current?.botId === botId ? loadedRef.current : null;
      const merged = mergeNewestTranscriptPage(prev, page);
      const running = Boolean(page.running);
      commit({
        botId,
        ...merged,
        running,
        reset: Boolean(page.reset),
        // The earlier history stays only while the merge kept it (the first message is unchanged).
        earlier: Boolean(prev?.earlier && merged.messages[0]?.id === prev.messages[0]?.id),
      });
      if (pendingRef.current && userCount(merged.messages) > pendingRef.current.base) {
        pendingRef.current = null;
        setPending(null);
      }
      // The stored reply takes the streamed one's place in this same render.
      const unrecorded = Boolean(pendingRef.current);
      const stored = unrecorded ? null : merged.messages;
      setLive((now) => afterRead(now, started, stored, !running && !unrecorded));
      // A message not recorded yet is still being answered, even before the engine says it runs.
      if (!running && !pendingRef.current && page.messages.at(-1)?.role !== "user") {
        setWaiting(false);
      }
    } catch {
      // Keep what is shown; the next event or send reads again. A Muse with no Conversation
      // yet simply shows none.
      if (current === generation.current && !loadedRef.current) {
        commit({
          botId,
          threadId: "",
          messages: [],
          olderCursor: null,
          reset: false,
          earlier: false,
          running: false,
        });
      }
    }
  }, [botId, commit]);

  const refresh = useCallback(() => {
    if (throttle.current !== undefined) return;
    throttle.current = window.setTimeout(() => {
      throttle.current = undefined;
      void read();
    }, REFRESH_THROTTLE_MS);
  }, [read]);

  const awaiting = waiting || Boolean(loaded?.running);
  const familyLive = useFamilyLive(botId);
  useEffect(() => {
    if (!awaiting || familyLive) return;
    const timer = window.setInterval(refresh, REPLY_POLL_MS);
    return () => window.clearInterval(timer);
  }, [awaiting, familyLive, refresh]);

  // A hidden tab shows no words being written: its reply stream closes (a browser holds six
  // connections per origin, shared by every tab) and reopens on return, where the engine replays
  // the reply so far.
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);

  // Looking at the Conversation reads it: on opening, when a reply lands while it is in view,
  // and when the person comes back to the window.
  const markSeen = useCallback(() => {
    if (!botId || document.visibilityState !== "visible" || !document.hasFocus()) return;
    void rpc.chats.markRead({ botId }).catch(() => undefined);
  }, [botId]);
  const markSeenRef = useRef(markSeen);
  markSeenRef.current = markSeen;
  useEffect(() => {
    const onBack = () => markSeenRef.current();
    onBack();
    window.addEventListener("focus", onBack);
    document.addEventListener("visibilitychange", onBack);
    return () => {
      window.removeEventListener("focus", onBack);
      document.removeEventListener("visibilitychange", onBack);
    };
  }, [botId]);

  useEffect(() => {
    generation.current += 1;
    commit(null);
    pendingRef.current = null;
    setPending(null);
    setWaiting(false);
    setStopped(false);
    setCancelled(false);
    setLive(NO_LIVE);
    if (!botId) return;
    firstRead.current = read();
    let connected = false;
    const stop = watchFamily(botId, (event) => {
      if (
        event.type === "messageDone" ||
        event.type === "turnDone" ||
        event.type === "messageDelivery"
      ) {
        const current = loadedRef.current;
        if (event.chatId === current?.threadId) {
          if (event.type === "messageDone") markSeenRef.current();
          if (event.type === "turnDone") {
            // The turn ended: it no longer runs, a Stop has done its work, and how it ended is
            // the engine's word. The read that follows says whether another turn started.
            commit({ ...current, running: false });
            setStopped(false);
            // The streamed reply stays until the read that follows brings the stored one.
            const at = reads.current;
            setLive((now) => applyReply(now, { kind: "textReset" }, false, at));
            // Cut short at a Stop it reads "incomplete": what the Stop said stands then.
            if (event.status !== "incomplete") setCancelled(event.status === "cancelled");
          }
          refresh();
        }
      } else if (event.type === "chatReset") {
        if (event.chatId === loadedRef.current?.threadId) {
          // Nothing loaded survives a clear: read the Conversation afresh.
          loadedRef.current = null;
          refresh();
        }
      } else if (event.type === "chatsChanged") {
        refresh();
      } else if (event.type === "open") {
        if (connected) refresh();
        connected = true;
      }
    });
    return () => {
      generation.current += 1;
      window.clearTimeout(throttle.current);
      throttle.current = undefined;
      stop();
    };
  }, [botId, commit, read, refresh]);

  // While a reply is expected and the tab is in view, its words as they are written (the
  // Conversation's own stream, the page's only one besides the family stream); reconnects shortly
  // after a drop, and closes when the turn ends or the tab is hidden.
  const streamChat = loaded?.botId === botId ? loaded?.threadId : undefined;
  useEffect(() => {
    if (!botId || !streamChat || !awaiting || !visible) return;
    const abort = new AbortController();
    void (async () => {
      let retryMs = REPLY_RETRY_MIN_MS;
      while (!abort.signal.aborted) {
        let replace = true;
        try {
          const events = await rpc.chats.watchReply(
            { botId, chatId: streamChat },
            { signal: abort.signal },
          );
          for await (const event of events) {
            if (abort.signal.aborted) return;
            retryMs = REPLY_RETRY_MIN_MS;
            const first = replace;
            replace = false;
            const at = reads.current;
            setLive((now) => applyReply(now, event, first, at));
            // The text was stored: a read that starts after this brings the stored copy and
            // drops the streamed one. Without it the streamed copy stays under the stored one
            // until some later event happens to read again.
            if (event.kind === "textReset") refresh();
          }
        } catch {
          // Dropped: try again shortly.
        }
        if (abort.signal.aborted) return;
        await new Promise((resolve) => window.setTimeout(resolve, retryMs));
        retryMs = Math.min(retryMs + REPLY_RETRY_MIN_MS, REPLY_RETRY_MAX_MS);
      }
    })();
    return () => abort.abort();
  }, [botId, streamChat, awaiting, visible, refresh]);

  useEffect(() => {
    if (!waiting) return;
    const timer = window.setTimeout(() => setWaiting(false), REPLY_WAIT_MAX_MS);
    return () => window.clearTimeout(timer);
  }, [waiting]);

  /** The person's message is on its way: show it at once and hold the Conversation as
   * running. */
  const beginSend = useCallback((text: string) => {
    const next: Pending = { text, base: userCount(loadedRef.current?.messages ?? null) };
    pendingRef.current = next;
    setPending(next);
    setWaiting(true);
    setStopped(false);
    setCancelled(false);
  }, []);

  /** The send did not go through: nothing is pending or awaited. */
  const failSend = useCallback(() => {
    pendingRef.current = null;
    setPending(null);
    setWaiting(false);
  }, []);

  /** A Stop landed: no reply is coming (though the last message may still be the person's, and
   * one already posted stays until the transcript has it), and nothing reads as running until the
   * turn's `turnDone` or the next send. */
  const stopWaiting = useCallback(() => {
    setWaiting(false);
    setStopped(true);
    setCancelled(true);
  }, []);

  const loadOlder = useCallback(async () => {
    const current = loadedRef.current;
    if (!botId || !current || current.botId !== botId || !current.olderCursor || loadingOlder) {
      return;
    }
    const element = scrollRef.current;
    const previousHeight = element?.scrollHeight ?? 0;
    const epoch = generation.current;
    setLoadingOlder(true);
    try {
      const page = await rpc.chats.transcript({
        botId,
        before: current.olderCursor,
        ...(current.earlier ? { beforeReset: true } : {}),
      });
      const latest = loadedRef.current;
      if (epoch !== generation.current || !latest) return;
      commit({
        ...latest,
        messages: [...page.messages, ...latest.messages],
        olderCursor: page.olderItemCursor ?? null,
      });
      window.requestAnimationFrame(() => {
        const next = scrollRef.current;
        if (next) next.scrollTop += next.scrollHeight - previousHeight;
      });
    } finally {
      setLoadingOlder(false);
    }
  }, [botId, commit, loadingOlder, scrollRef]);

  /** Reads the history from before the latest clear and shows it above the Conversation. */
  const showEarlier = useCallback(async () => {
    const current = loadedRef.current;
    if (!botId || !current || current.botId !== botId || !current.reset || current.earlier) return;
    const epoch = generation.current;
    setLoadingOlder(true);
    try {
      const page = await rpc.chats.transcript({ botId, beforeReset: true });
      const latest = loadedRef.current;
      if (epoch !== generation.current || !latest) return;
      commit({
        ...latest,
        messages: [...page.messages, ...latest.messages],
        olderCursor: page.olderItemCursor ?? null,
        earlier: true,
      });
    } finally {
      setLoadingOlder(false);
    }
  }, [botId, commit]);

  /** Pages back until the message is loaded (a search hit can be far up); false if it is not
   * in the Conversation. */
  const reveal = useCallback(
    async (messageId: string): Promise<boolean> => {
      await firstRead.current;
      const epoch = generation.current;
      for (let pages = 0; pages < MAX_REVEAL_PAGES; pages += 1) {
        const current = loadedRef.current;
        if (!botId || !current || current.botId !== botId) return false;
        if (current.messages.some((message) => message.id === messageId)) return true;
        if (!current.olderCursor) {
          if (!current.reset || current.earlier) return false;
          await showEarlier();
          continue;
        }
        const page = await rpc.chats.transcript({
          botId,
          before: current.olderCursor,
          ...(current.earlier ? { beforeReset: true } : {}),
        });
        const latest = loadedRef.current;
        if (epoch !== generation.current || !latest) return false;
        commit({
          ...latest,
          messages: [...page.messages, ...latest.messages],
          olderCursor: page.olderItemCursor ?? null,
        });
      }
      return false;
    },
    [botId, commit, showEarlier],
  );

  const forBot = loaded?.botId === botId ? loaded : null;
  const threadId = forBot?.threadId || null;
  const pendingMessage = useMemo(
    () => (pending ? optimisticMessage(threadId ?? "", pending.text) : null),
    [pending, threadId],
  );
  const liveText = joinText(live.settled, live.text);
  const liveMessage = useMemo<ThreadMessage | null>(
    () =>
      liveText && threadId
        ? {
            id: `live:${threadId}`,
            threadId,
            seq: Number.MAX_SAFE_INTEGER,
            role: "bot",
            blocks: [{ kind: "text", text: liveText }],
            createdAt: new Date().toISOString(),
          }
        : null,
    [liveText, threadId],
  );
  const engineRunning = Boolean(forBot?.running);
  const turn = useMemo<TurnStatus>(
    () => ({ running: engineRunning, awaiting: waiting, stopped, cancelled }),
    [engineRunning, waiting, stopped, cancelled],
  );
  return {
    /** `null` until the first page has been read. */
    messages: forBot?.messages ?? null,
    /** The Conversation's engine id, or `null` until it is read. */
    threadId,
    /** The person's message sent but not yet in the transcript. */
    pendingMessage,
    /** The reply as it streams, until the transcript has it: shown after `pendingMessage`. */
    liveMessage,
    /** The turn's status as the engine reported it, for the work state (features/activity
     * `useWorkState`): the one reading of whether the Conversation runs. */
    turn,
    beginSend,
    failSend,
    stopWaiting,
    /** The cursor of the next older page, or `null` when this is the start. */
    olderCursor: forBot?.olderCursor ?? null,
    loadingOlder,
    loadOlder,
    /** The Conversation was cleared and its earlier history is not shown yet. */
    canShowEarlier: Boolean(forBot?.reset && !forBot.earlier),
    showEarlier,
    reveal,
    refresh,
  };
}
