import type { HostStatus } from "@superchat/contracts";

/** How long a step waits for a host that is starting (the managed launch window). */
export const HOST_LAUNCH_WINDOW_MS = 120_000;

/** The host's status as a step that waits on it sees it: it changes only while a step listens. */
export interface HostFeed {
  current(): HostStatus | null;
  subscribe(listener: (status: HostStatus | null) => void): () => void;
}

/** How often a waiting step reads the host's status. */
const HOST_STATUS_POLL_MS = 3_000;

/** A feed that reads the host's status (at once, then every few seconds) only while someone
 * is subscribed, so nothing polls while no step is waiting. */
export function pollingHostFeed(
  read: () => Promise<HostStatus | null>,
  intervalMs: number = HOST_STATUS_POLL_MS,
): HostFeed {
  let latest: HostStatus | null = null;
  let timer: ReturnType<typeof setInterval> | undefined;
  const listeners = new Set<(status: HostStatus | null) => void>();
  const poll = () =>
    void read().then(
      (status) => {
        latest = status;
        for (const listener of listeners) listener(status);
      },
      () => undefined,
    );
  return {
    current: () => latest,
    subscribe(listener) {
      listeners.add(listener);
      if (listeners.size === 1) {
        poll();
        timer = setInterval(poll, intervalMs);
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          clearInterval(timer);
          timer = undefined;
        }
      };
    },
  };
}

/** Up: running, its runner connected (or not known to be missing), and no launch in progress. */
export function hostIsUp(status: HostStatus | null): boolean {
  return Boolean(
    status && status.state === "running" && status.runnerReady !== false && !status.launch,
  );
}

/** The launch failed, so waiting longer cannot help. */
function launchFailed(status: HostStatus | null): boolean {
  return status?.launch?.stage === "failed" || status?.state === "error";
}

export class HostStartTimeout extends Error {
  constructor() {
    super("Your machine did not start in time");
    this.name = "HostStartTimeout";
  }
}

/** Resolves when a status event says the host is up. Rejects when `signal` aborts, when the
 * launch fails, or when `windowMs` passes. */
export function waitForHostUp(
  feed: HostFeed,
  signal: AbortSignal,
  windowMs: number = HOST_LAUNCH_WINDOW_MS,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const finish = (done: () => void) => {
      clearTimeout(timer);
      unsubscribe();
      signal.removeEventListener("abort", onAbort);
      done();
    };
    const onAbort = () => finish(() => reject(signal.reason));
    const react = (status: HostStatus | null) => {
      if (hostIsUp(status)) finish(resolve);
      else if (launchFailed(status)) finish(() => reject(new HostStartTimeout()));
    };
    // An event that landed before this wait began is not lost: look at the status as it is now.
    const unsubscribe = feed.subscribe(react);
    const timer = setTimeout(() => finish(() => reject(new HostStartTimeout())), windowMs);
    signal.addEventListener("abort", onAbort, { once: true });
    react(feed.current());
  });
}

/**
 * Runs one step that needs the host (a workspace write). If the engine answers "starting",
 * `onStarting` fires, the step waits for the status event that says the host is up (or for
 * `signal` to abort), then runs once more. No timer retries.
 */
export async function runWhenHostReady<T>(
  attempt: () => Promise<T>,
  options: {
    isStarting: (error: unknown) => boolean;
    onStarting: () => void;
    feed: HostFeed;
    signal: AbortSignal;
    windowMs?: number;
  },
): Promise<T> {
  options.signal.throwIfAborted();
  try {
    return await attempt();
  } catch (error) {
    if (!options.isStarting(error)) throw error;
    options.onStarting();
    try {
      await waitForHostUp(options.feed, options.signal, options.windowMs);
    } catch (waitError) {
      if (waitError instanceof HostStartTimeout) throw error;
      throw waitError;
    }
  }
  return attempt();
}

/** Calls `onStarting` / `onUp` as status events say the host goes down or up while a call
 * that wakes it (the engine's ingest) is in flight. Returns the unsubscribe. */
export function followHostStart(
  feed: HostFeed,
  handlers: { onStarting: () => void; onUp: () => void },
): () => void {
  const apply = (status: HostStatus | null) => {
    if (status === null) return;
    if (hostIsUp(status)) handlers.onUp();
    else handlers.onStarting();
  };
  apply(feed.current());
  return feed.subscribe(apply);
}
