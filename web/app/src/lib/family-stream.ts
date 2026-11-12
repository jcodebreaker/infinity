import type { ActivityChanged, FamilyEvent } from "@superchat/contracts";
import { useEffect, useState } from "react";
import { rpc } from "./rpc";

/** Opens the Muse's family stream (`chats.watch`). A seam so tests and fixtures can supply
 * their own events instead of the live relay. */
export type FamilyWatch = (
  input: { botId: string },
  signal: AbortSignal,
) => Promise<AsyncIterable<FamilyEvent>>;

const liveWatch: FamilyWatch = (input, signal) => rpc.chats.watch(input, { signal });

const RETRY_MIN_MS = 1_000;
const RETRY_MAX_MS = 15_000;

type Listener = (event: FamilyEvent) => void;
type LiveListener = (live: boolean) => void;

/**
 * One live stream per Muse for the Conversation and its Helpers (ADR 0009). Events
 * carry ids only: every listener refetches what it shows. The stream opens with the first
 * listener and closes with the last; it reconnects with backoff and tells listeners `open` each
 * time it is (re)connected, so whatever was missed meanwhile is refetched.
 */
class FamilyStream {
  private readonly listeners = new Set<Listener>();
  private readonly liveListeners = new Set<LiveListener>();
  private abort: AbortController | undefined;
  /** Connected right now: listeners hear every change as it happens. */
  live = false;

  constructor(
    private readonly botId: string,
    private readonly watch: FamilyWatch,
    private readonly onIdle: () => void,
  ) {}

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    if (this.listeners.size === 1) this.start();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) {
        this.abort?.abort();
        this.abort = undefined;
        this.setLive(false);
        this.onIdle();
      }
    };
  }

  /** Hears whether the stream is connected (only while also subscribed). */
  watchLive(listener: LiveListener): () => void {
    this.liveListeners.add(listener);
    listener(this.live);
    return () => this.liveListeners.delete(listener);
  }

  private setLive(live: boolean): void {
    if (this.live === live) return;
    this.live = live;
    for (const listener of [...this.liveListeners]) listener(live);
  }

  private emit(event: FamilyEvent): void {
    if (event.type === "open") this.setLive(true);
    for (const listener of [...this.listeners]) listener(event);
  }

  private start(): void {
    const abort = new AbortController();
    this.abort = abort;
    void (async () => {
      let retryMs = RETRY_MIN_MS;
      while (!abort.signal.aborted) {
        try {
          const events = await this.watch({ botId: this.botId }, abort.signal);
          for await (const event of events) {
            if (abort.signal.aborted) return;
            retryMs = RETRY_MIN_MS;
            this.emit(event);
          }
        } catch {
          if (abort.signal.aborted) return;
        }
        this.setLive(false);
        await new Promise((resolve) => setTimeout(resolve, retryMs));
        retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
      }
    })();
  }
}

const streams = new Map<string, FamilyStream>();

/** Listens to a Muse's family stream; returns the unsubscribe. */
export function watchFamily(
  botId: string,
  listener: Listener,
  watch: FamilyWatch = liveWatch,
): () => void {
  let stream = streams.get(botId);
  if (!stream) {
    const created = new FamilyStream(botId, watch, () => {
      if (streams.get(botId) === created) streams.delete(botId);
    });
    streams.set(botId, created);
    stream = created;
  }
  return stream.subscribe(listener);
}

/**
 * Whether the Muse's family stream is connected (and keeps it open while mounted). While it is,
 * every change reaches the page as it happens, so a poll that only catches missed changes can
 * rest; on each reconnect listeners hear `open` and read again.
 */
export function useFamilyLive(botId: string | null | undefined): boolean {
  const [live, setLive] = useState(false);
  useEffect(() => {
    if (!botId) return;
    const stop = watchFamily(botId, () => undefined);
    const stream = streams.get(botId);
    const unwatch = stream?.watchLive(setLive);
    return () => {
      unwatch?.();
      stop();
      setLive(false);
    };
  }, [botId]);
  return live;
}

/**
 * The Activity Feed's "the feed changed" signal, read off the Muse's family stream instead of a
 * stream of its own. A browser keeps only six connections open per origin over HTTP/1.1; one
 * long-lived stream per tab rather than two leaves room for a few tabs before every other request
 * (a send, a file read) queues behind them. A (re)connect counts as a change: whatever happened
 * while the stream was down is read again.
 */
export function familyActivityChanges(
  input: { botId: string },
  signal: AbortSignal,
  watch: FamilyWatch = liveWatch,
): Promise<AsyncIterable<ActivityChanged>> {
  const queue: ActivityChanged[] = [];
  let wake: (() => void) | undefined;
  const stop = watchFamily(
    input.botId,
    (event) => {
      if (event.type === "activitiesChanged" || event.type === "open") {
        queue.push({ type: "changed" });
      } else if (event.type === "heartbeat") {
        queue.push({ type: "heartbeat" });
      } else return;
      wake?.();
    },
    watch,
  );
  const close = () => {
    stop();
    wake?.();
  };
  if (signal.aborted) close();
  else signal.addEventListener("abort", close, { once: true });
  return Promise.resolve(
    (async function* () {
      while (!signal.aborted) {
        const next = queue.shift();
        if (next) yield next;
        else await new Promise<void>((resolve) => (wake = resolve));
      }
    })(),
  );
}
