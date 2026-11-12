import type { HostStatus } from "@superchat/contracts";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { pollingHostFeed } from "./hostReady";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const status = (state: HostStatus["state"]) => ({ state }) as HostStatus;

it("reads the host's status only while a step listens, at once and then on an interval", async () => {
  const read = vi.fn(async () => status("booting"));
  const feed = pollingHostFeed(read, 1_000);
  await vi.advanceTimersByTimeAsync(5_000);
  expect(read).not.toHaveBeenCalled();

  const seen: Array<HostStatus | null> = [];
  const unsubscribe = feed.subscribe((next) => seen.push(next));
  await vi.advanceTimersByTimeAsync(0);
  expect(read).toHaveBeenCalledTimes(1);
  expect(feed.current()?.state).toBe("booting");

  read.mockResolvedValue(status("running"));
  await vi.advanceTimersByTimeAsync(1_000);
  expect(seen.map((next) => next?.state)).toEqual(["booting", "running"]);

  unsubscribe();
  await vi.advanceTimersByTimeAsync(5_000);
  expect(read).toHaveBeenCalledTimes(2);
});

it("keeps what it last saw when a read fails", async () => {
  const read = vi.fn<() => Promise<HostStatus | null>>();
  read.mockResolvedValueOnce(status("running")).mockRejectedValue(new Error("down"));
  const feed = pollingHostFeed(read, 1_000);
  const unsubscribe = feed.subscribe(() => undefined);
  await vi.advanceTimersByTimeAsync(2_000);
  expect(feed.current()?.state).toBe("running");
  unsubscribe();
});
