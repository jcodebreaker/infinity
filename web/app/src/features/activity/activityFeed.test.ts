// @vitest-environment jsdom

import type { ActivityChanged, ActivityPage } from "@superchat/contracts";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ActivityFeed, type ActivityFeedDeps } from "./activityFeed";
import { ACTIVITY_POLL_ACTIVE_MS, ACTIVITY_POLL_IDLE_MS } from "./activityGrouping";
import { activity, helper, step } from "./activityTestKit";

/** A controllable `activities.watch`: push frames in, end or fail the stream. */
function fakeWatch() {
  let push: ((frame: ActivityChanged) => void) | undefined;
  let fail: ((error: unknown) => void) | undefined;
  const watch = vi.fn(async (_input: { botId: string }, signal: AbortSignal) => {
    const queue: ActivityChanged[] = [];
    let wake: (() => void) | undefined;
    let failure: unknown;
    push = (frame) => {
      queue.push(frame);
      wake?.();
    };
    fail = (error) => {
      failure = error;
      wake?.();
    };
    signal.addEventListener("abort", () => wake?.());
    return (async function* () {
      while (!signal.aborted) {
        if (failure) throw failure;
        const frame = queue.shift();
        if (frame) yield frame;
        else await new Promise<void>((resolve) => (wake = resolve));
      }
    })();
  });
  return { watch, push: (f: ActivityChanged) => push?.(f), fail: (e: unknown) => fail?.(e) };
}

const page = (...activities: ActivityPage["activities"]): ActivityPage => ({
  activities,
  hasMore: false,
});

let visibility: "visible" | "hidden";
beforeEach(() => {
  vi.useFakeTimers();
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
});
afterEach(() => vi.useRealTimers());

function setup(pages: ActivityPage[], watch = fakeWatch()) {
  const list = vi.fn(async () => pages.shift() ?? pages.at(-1) ?? page());
  const deps: ActivityFeedDeps = { list, watch: watch.watch, isUnavailable: () => false };
  const feed = new ActivityFeed("bot-1", deps);
  const states: string[] = [];
  const unsubscribe = feed.subscribe(() => states.push(feed.getSnapshot().state.status));
  return { feed, list, watch, states, unsubscribe };
}

const ready = (feed: ActivityFeed) => {
  const { state } = feed.getSnapshot();
  if (state.status !== "ready") throw new Error(`feed is ${state.status}`);
  return state.activities;
};

it("shows a Helper running the moment the engine says the feed changed, without waiting for a poll", async () => {
  const running = helper("h1", { status: "in_progress", finishedAt: null, steps: [] });
  const { feed, list, watch } = setup([page(), page(running)]);
  await vi.advanceTimersByTimeAsync(0);
  expect(ready(feed)).toEqual([]);

  watch.push({ type: "changed" });
  await vi.advanceTimersByTimeAsync(200);

  expect(list).toHaveBeenCalledTimes(2);
  expect(ready(feed).map((a) => [a.chatId, a.status])).toEqual([["h1", "in_progress"]]);
});

it("updates a Helper in place as it takes steps and then finishes, never duplicating its row", async () => {
  const at = (n: number, status: "in_progress" | "done") =>
    helper("h1", {
      status,
      finishedAt: status === "done" ? "2026-10-03T11:30:00.000Z" : null,
      steps: Array.from({ length: n }, (_, i) => step(`Step ${i + 1}`, i + 1)),
    });
  const { feed, watch } = setup([
    page(at(1, "in_progress")),
    page(at(3, "in_progress")),
    page(at(3, "done")),
  ]);
  await vi.advanceTimersByTimeAsync(0);
  expect(ready(feed)[0]?.steps).toHaveLength(1);

  watch.push({ type: "changed" });
  await vi.advanceTimersByTimeAsync(200);
  expect(ready(feed)).toHaveLength(1);
  expect(ready(feed)[0]?.steps).toHaveLength(3);

  watch.push({ type: "changed" });
  await vi.advanceTimersByTimeAsync(200);
  expect(ready(feed)).toHaveLength(1);
  expect(ready(feed)[0]?.status).toBe("done");
});

it("folds a burst of signals into one read", async () => {
  const { list, watch } = setup([page()]);
  await vi.advanceTimersByTimeAsync(0);
  for (let i = 0; i < 5; i += 1) watch.push({ type: "changed" });
  await vi.advanceTimersByTimeAsync(300);
  expect(list).toHaveBeenCalledTimes(2);
});

it("polls tightly while something runs even with the signal connected, as the safety net", async () => {
  const running = helper("h1", { status: "in_progress", finishedAt: null });
  const { list } = setup([page(running)]);
  await vi.advanceTimersByTimeAsync(0);
  expect(list).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(ACTIVITY_POLL_ACTIVE_MS + 10);
  expect(list).toHaveBeenCalledTimes(2);
});

it("falls back to plain polling when the live signal is unavailable", async () => {
  const watch = fakeWatch();
  const list = vi.fn(async () => page(activity()));
  const unavailable = new Error("NOT_IMPLEMENTED");
  watch.watch.mockRejectedValue(unavailable);
  const feed = new ActivityFeed("bot-1", {
    list,
    watch: watch.watch,
    isUnavailable: (error) => error === unavailable,
  });
  feed.subscribe(() => undefined);
  await vi.advanceTimersByTimeAsync(0);
  expect(ready(feed)).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(ACTIVITY_POLL_IDLE_MS + 10);
  expect(list.mock.calls.length).toBeGreaterThanOrEqual(2);
});

it("reconnects the signal after it drops and reads again", async () => {
  const { watch, list } = setup([page()]);
  await vi.advanceTimersByTimeAsync(0);
  watch.fail(new Error("network"));
  await vi.advanceTimersByTimeAsync(1_100);
  expect(watch.watch).toHaveBeenCalledTimes(2);
  watch.push({ type: "changed" });
  await vi.advanceTimersByTimeAsync(200);
  expect(list.mock.calls.length).toBeGreaterThanOrEqual(2);
});

it("stays live in a tab first opened hidden: the Helper settles without the tab coming back", async () => {
  // The live run: a covered window reports "hidden" from the start; the feed read once, never
  // opened the signal and never polled, so a finished Helper stayed "Working now" for minutes.
  visibility = "hidden";
  const running = helper("h1", { status: "in_progress", finishedAt: null });
  const done = helper("h1", { status: "done" });
  const second = helper("h2", { status: "in_progress", finishedAt: null });
  const { feed, list, watch } = setup([page(running), page(done, second)]);
  await vi.advanceTimersByTimeAsync(0);
  expect(list).toHaveBeenCalledTimes(1);
  expect(watch.watch).toHaveBeenCalledTimes(1);

  watch.push({ type: "changed" });
  await vi.advanceTimersByTimeAsync(200);
  const rows = ready(feed).map((a) => [a.chatId, a.status]);
  expect(rows.sort()).toEqual([
    ["h1", "done"],
    ["h2", "in_progress"],
  ]);
});

it("keeps the safety-net poll tight in a hidden tab while something is running", async () => {
  visibility = "hidden";
  const running = helper("h1", { status: "in_progress", finishedAt: null });
  const { list } = setup([page(running)]);
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(ACTIVITY_POLL_ACTIVE_MS * 2 + 10);
  expect(list).toHaveBeenCalledTimes(3);
});

it("reads at once when a hidden tab comes back into view", async () => {
  const { list } = setup([page()]);
  await vi.advanceTimersByTimeAsync(0);
  visibility = "hidden";
  document.dispatchEvent(new Event("visibilitychange"));
  await vi.advanceTimersByTimeAsync(0);
  expect(list).toHaveBeenCalledTimes(1);

  visibility = "visible";
  document.dispatchEvent(new Event("visibilitychange"));
  await vi.advanceTimersByTimeAsync(200);
  expect(list).toHaveBeenCalledTimes(2);
});

it("turns a read that never answers into an error, not an endless loading state", async () => {
  const list = vi.fn(() => new Promise<ActivityPage>(() => undefined));
  const feed = new ActivityFeed("bot-1", {
    list,
    watch: fakeWatch().watch,
    isUnavailable: () => false,
  });
  feed.subscribe(() => undefined);
  await vi.advanceTimersByTimeAsync(0);
  expect(feed.getSnapshot().state.status).toBe("loading");
  await vi.advanceTimersByTimeAsync(21_000);
  expect(feed.getSnapshot().state.status).toBe("error");
});

it("shares one feed between the panel and the chat rows: the last one out closes it", async () => {
  const { feed, list, watch, unsubscribe } = setup([page()]);
  const second = feed.subscribe(() => undefined);
  await vi.advanceTimersByTimeAsync(0);
  expect(list).toHaveBeenCalledTimes(1);
  expect(watch.watch).toHaveBeenCalledTimes(1);

  unsubscribe();
  second();
  await vi.advanceTimersByTimeAsync(ACTIVITY_POLL_IDLE_MS * 3);
  expect(list).toHaveBeenCalledTimes(1);
});

it("keeps older pages loaded when the newest page is read again", async () => {
  const newest = activity({ id: "new", startedAt: "2026-10-03T12:00:00.000Z" });
  const old = activity({ id: "old", startedAt: "2026-10-01T12:00:00.000Z" });
  const pages = [
    { activities: [newest], hasMore: true },
    { activities: [old], hasMore: false },
  ];
  const list = vi.fn(async () => pages.shift() ?? { activities: [newest], hasMore: true });
  const feed = new ActivityFeed("bot-1", {
    list,
    watch: fakeWatch().watch,
    isUnavailable: () => false,
  });
  feed.subscribe(() => undefined);
  await vi.advanceTimersByTimeAsync(0);
  await feed.loadEarlier();
  expect(ready(feed).map((a) => a.id)).toEqual(["new", "old"]);

  await vi.advanceTimersByTimeAsync(ACTIVITY_POLL_IDLE_MS + 10);
  expect(ready(feed).map((a) => a.id)).toEqual(["new", "old"]);
});

it("reports an unreadable feed as unavailable or an error, and recovers on the next read", async () => {
  const gone = new Error("gone");
  let failing: unknown = gone;
  const list = vi.fn(async () => {
    if (failing) throw failing;
    return page(activity());
  });
  const feed = new ActivityFeed("bot-1", {
    list,
    watch: fakeWatch().watch,
    isUnavailable: (error) => error === gone,
  });
  feed.subscribe(() => undefined);
  await vi.advanceTimersByTimeAsync(0);
  expect(feed.getSnapshot().state.status).toBe("unavailable");

  failing = null;
  await vi.advanceTimersByTimeAsync(ACTIVITY_POLL_IDLE_MS + 10);
  expect(feed.getSnapshot().state.status).toBe("ready");
});
