import type { FamilyEvent } from "@superchat/contracts";
import { describe, expect, it, vi } from "vitest";
import { familyActivityChanges, watchFamily } from "./family-stream";

function source(...events: FamilyEvent[]) {
  const watch = vi.fn(async (_input: { botId: string }, signal: AbortSignal) =>
    (async function* () {
      for (const event of events) yield event;
      // The relay stays open until the last listener leaves.
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve()));
    })(),
  );
  return watch;
}

describe("watchFamily", () => {
  it("shares one stream per Muse between listeners and closes it with the last", async () => {
    const watch = source({ type: "open" }, { type: "chatsChanged" });
    const first = vi.fn();
    const second = vi.fn();
    const stopFirst = watchFamily("bot-a", first, watch);
    const stopSecond = watchFamily("bot-a", second, watch);

    await vi.waitFor(() => expect(first).toHaveBeenCalledTimes(2));
    expect(watch).toHaveBeenCalledTimes(1);
    expect(second.mock.calls.map(([event]) => event.type)).toEqual(["open", "chatsChanged"]);
    const signal = watch.mock.calls[0]?.[1];

    stopFirst();
    expect(signal?.aborted).toBe(false);
    stopSecond();
    expect(signal?.aborted).toBe(true);
  });

  it("reconnects after the stream ends", async () => {
    vi.useFakeTimers();
    try {
      let attempts = 0;
      const watch = vi.fn(async () => {
        attempts += 1;
        return (async function* () {
          yield { type: "open" } as const;
        })();
      });
      const listener = vi.fn();
      const stop = watchFamily("bot-b", listener, watch);
      await vi.advanceTimersByTimeAsync(1_100);
      expect(attempts).toBeGreaterThanOrEqual(2);
      expect(listener).toHaveBeenCalledWith({ type: "open" });
      stop();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("familyActivityChanges", () => {
  it("reads the feed signal off the family stream the tab already holds", async () => {
    const watch = source(
      { type: "open" },
      { type: "chatsChanged" },
      { type: "activitiesChanged" },
      { type: "heartbeat" },
    );
    const conversation = vi.fn();
    const stopConversation = watchFamily("bot-c", conversation, watch);
    const abort = new AbortController();
    const frames: string[] = [];
    const reading = (async () => {
      for await (const frame of await familyActivityChanges(
        { botId: "bot-c" },
        abort.signal,
        watch,
      ))
        frames.push(frame.type);
    })();

    await vi.waitFor(() => expect(frames).toEqual(["changed", "changed", "heartbeat"]));
    // One connection for both: the Conversation's stream carries the feed's signal too.
    expect(watch).toHaveBeenCalledTimes(1);
    const signal = watch.mock.calls[0]?.[1];

    abort.abort();
    await reading;
    expect(signal?.aborted).toBe(false);
    stopConversation();
    expect(signal?.aborted).toBe(true);
  });

  it("counts a (re)connect as a change and closes the stream when it is the last reader", async () => {
    const watch = source({ type: "open" });
    const abort = new AbortController();
    const frames: string[] = [];
    const reading = (async () => {
      for await (const frame of await familyActivityChanges(
        { botId: "bot-d" },
        abort.signal,
        watch,
      ))
        frames.push(frame.type);
    })();

    await vi.waitFor(() => expect(frames).toEqual(["changed"]));
    abort.abort();
    await reading;
    expect(watch.mock.calls[0]?.[1].aborted).toBe(true);
  });
});
