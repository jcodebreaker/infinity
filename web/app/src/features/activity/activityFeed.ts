import type { Activity, ActivityChanged, ActivityPage } from "@superchat/contracts";
import { activitiesPollIntervalMs, mergeNewestPage } from "./activityGrouping";

/** The Activity feed's wire: a Muse's Activity Feed, or why it can't be shown. */
export type ActivitiesState =
  | { status: "loading" }
  | { status: "ready"; activities: Activity[]; hasMore: boolean }
  | { status: "unavailable" }
  | { status: "error" };

export interface ActivityFeedSnapshot {
  state: ActivitiesState;
  loadingEarlier: boolean;
}

export interface ActivityFeedDeps {
  list: (input: { botId: string; before?: number; limit?: number }) => Promise<ActivityPage>;
  /** Opens the engine's live "the feed changed" signal (the family stream's `activitiesChanged`). */
  watch: (input: { botId: string }, signal: AbortSignal) => Promise<AsyncIterable<ActivityChanged>>;
  /** True for "this Muse has no Conversation / no backend yet": calm empty, not an error. */
  isUnavailable: (error: unknown) => boolean;
}

const PAGE_SIZE = 30;
/** Signals arrive in bursts (a step, its status edge, a title); read once per burst. */
const REFRESH_THROTTLE_MS = 150;
/** A read that has not answered by now is an error, never an endless "Loading…". */
const LIST_TIMEOUT_MS = 20_000;
const WATCH_RETRY_MIN_MS = 1_000;
const WATCH_RETRY_MAX_MS = 15_000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("activities read timed out")), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function epochSeconds(iso: string): number {
  return Math.floor(new Date(iso).getTime() / 1000);
}

/**
 * One Muse's Activity Feed, shared by everything that shows it (the Activity panel, the Helper
 * rows in the chat) so they can never disagree. While anyone is subscribed it re-reads when the
 * engine says the feed changed (`watch`: a Helper started, took a step, settled), with a poll
 * only as the safety net under that signal — tight while something is running, slow otherwise,
 * and the whole cadence if the signal is unavailable.
 *
 * It stays live while the tab is hidden: browsers report a page "hidden" whenever its window is
 * covered or in the background (often the window the person glances back at, or one driven by
 * automation), and pausing there left a Helper "Working now" long after it had finished. The
 * signal rides the family stream the tab already holds and the browser throttles hidden timers; coming back into
 * view only adds an immediate re-read.
 */
export class ActivityFeed {
  private snapshot: ActivityFeedSnapshot = { state: { status: "loading" }, loadingEarlier: false };
  private readonly listeners = new Set<() => void>();
  private loading = false;
  private reloadQueued = false;
  private pollTimer: ReturnType<typeof setTimeout> | undefined;
  private throttleTimer: ReturnType<typeof setTimeout> | undefined;
  private watchAbort: AbortController | undefined;
  private watching = false;

  constructor(
    private readonly botId: string,
    private readonly deps: ActivityFeedDeps,
  ) {}

  getSnapshot = (): ActivityFeedSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) this.start();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.stop();
    };
  };

  /** Ask for a re-read soon (coalesced). Safe to call from anywhere, any number of times. */
  refresh = (): void => {
    if (!this.active() || this.throttleTimer !== undefined) return;
    this.throttleTimer = setTimeout(() => {
      this.throttleTimer = undefined;
      void this.load();
    }, REFRESH_THROTTLE_MS);
  };

  loadEarlier = async (): Promise<void> => {
    const { state, loadingEarlier } = this.snapshot;
    if (state.status !== "ready" || !state.hasMore || loadingEarlier) return;
    const oldest = state.activities.at(-1);
    if (!oldest) return;
    this.set({ ...this.snapshot, loadingEarlier: true });
    try {
      const page = await this.deps.list({
        botId: this.botId,
        before: epochSeconds(oldest.startedAt),
        limit: PAGE_SIZE,
      });
      const current = this.snapshot.state;
      if (current.status === "ready") {
        this.set({
          state: {
            status: "ready",
            activities: [...current.activities, ...page.activities],
            hasMore: page.hasMore,
          },
          loadingEarlier: false,
        });
        return;
      }
    } catch {
      // Keep what is shown; "Load earlier" stays, so trying again is a click away.
    }
    this.set({ ...this.snapshot, loadingEarlier: false });
  };

  private active(): boolean {
    return this.listeners.size > 0;
  }

  private set(next: ActivityFeedSnapshot): void {
    this.snapshot = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Back in view: whatever the throttled background timers missed is read at once. */
  private readonly onVisibilityChange = (): void => {
    if (document.visibilityState === "visible") this.refresh();
  };

  private start(): void {
    document.addEventListener("visibilitychange", this.onVisibilityChange);
    // A failed or unavailable earlier read is retried visibly; a ready feed stays put (no flash).
    if (this.snapshot.state.status !== "ready" && this.snapshot.state.status !== "loading") {
      this.set({ ...this.snapshot, state: { status: "loading" } });
    }
    void this.load();
    this.ensureWatch();
  }

  private stop(): void {
    document.removeEventListener("visibilitychange", this.onVisibilityChange);
    clearTimeout(this.pollTimer);
    clearTimeout(this.throttleTimer);
    this.throttleTimer = undefined;
    this.watchAbort?.abort();
    this.watchAbort = undefined;
    this.watching = false;
  }

  /** Reads the newest page, then schedules the next safety-net read. */
  private async load(): Promise<void> {
    if (!this.active()) return;
    if (this.loading) {
      this.reloadQueued = true;
      return;
    }
    this.loading = true;
    try {
      const page = await withTimeout(
        this.deps.list({ botId: this.botId, limit: PAGE_SIZE }),
        LIST_TIMEOUT_MS,
      );
      const prev = this.snapshot.state;
      // Only ever the newest page: merge it into whatever is loaded (which may include older
      // pages from "Load earlier") and leave `hasMore` as it was — it describes pagination
      // past the oldest loaded Activity, which this read says nothing about.
      this.set({
        ...this.snapshot,
        state:
          prev.status === "ready"
            ? {
                status: "ready",
                activities: mergeNewestPage(prev.activities, page.activities),
                hasMore: prev.hasMore,
              }
            : { status: "ready", activities: page.activities, hasMore: page.hasMore },
      });
      this.ensureWatch();
    } catch (error) {
      this.set({
        ...this.snapshot,
        state: { status: this.deps.isUnavailable(error) ? "unavailable" : "error" },
      });
    } finally {
      this.loading = false;
      if (this.reloadQueued) {
        this.reloadQueued = false;
        this.refresh();
      } else {
        this.schedulePoll();
      }
    }
  }

  private schedulePoll(): void {
    clearTimeout(this.pollTimer);
    if (!this.active()) return;
    const { state } = this.snapshot;
    const interval = activitiesPollIntervalMs(
      state.status === "ready" ? state.activities : [],
      this.watching,
    );
    this.pollTimer = setTimeout(() => void this.load(), interval);
  }

  /** Keeps one live signal open while active; reconnects with backoff, gives up quietly when
   * the backend has none (the poll carries on) and is re-tried by the next successful read. */
  private ensureWatch(): void {
    if (this.watchAbort || !this.active()) return;
    const abort = new AbortController();
    this.watchAbort = abort;
    void (async () => {
      let retryMs = WATCH_RETRY_MIN_MS;
      while (!abort.signal.aborted) {
        try {
          const frames = await this.deps.watch({ botId: this.botId }, abort.signal);
          for await (const frame of frames) {
            if (abort.signal.aborted) return;
            retryMs = WATCH_RETRY_MIN_MS;
            this.watching = true;
            if (frame.type === "changed") this.refresh();
          }
        } catch (error) {
          if (abort.signal.aborted) return;
          if (this.deps.isUnavailable(error)) break;
        }
        this.watching = false;
        await new Promise((resolve) => setTimeout(resolve, retryMs));
        retryMs = Math.min(retryMs * 2, WATCH_RETRY_MAX_MS);
      }
      if (this.watchAbort === abort) this.watchAbort = undefined;
      this.watching = false;
    })();
  }
}
