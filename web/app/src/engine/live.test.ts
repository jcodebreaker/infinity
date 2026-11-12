import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  run: vi.fn(),
  undo: vi.fn(),
  accept: vi.fn(),
  reset: vi.fn(),
  publish: vi.fn(),
}));

vi.mock("@superchat/adapters/omnigent/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@superchat/adapters/omnigent/browser")>()),
  getOmnigentLive: api.get,
  putOmnigentLive: api.put,
  deleteOmnigentLive: api.del,
  runOmnigentLive: api.run,
  undoOmnigentLive: api.undo,
  acceptOmnigentLive: api.accept,
  resetOmnigentLive: api.reset,
  publishOmnigentLive: api.publish,
}));
vi.mock("./client", () => ({
  engineClient: () => "client",
  museSessionId: async () => "s1",
  NO_EMAIL: "",
}));

const { liveRouter } = await import("./live");
const { call } = await import("@orpc/server");

const artifactId = "a1";
const row = (over: Record<string, unknown> = {}) => ({
  task_id: "t1",
  rrule: "FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=0",
  timezone: "UTC",
  schedule: "active",
  last_run_at: 1,
  sources: ["https://example.com"],
  tell: "notable",
  follow_publish: true,
  on_change: true,
  note: "prices only",
  held: null,
  checked_at: 100,
  open_issue: null,
  last_update: {
    version: 3,
    applied: [{ id: "total", before: "1", after: "2", source: null, notable: true }],
    suggestions: [{ id: "name", yours: "A", source_value: "B", source: null }],
    at: 90,
  },
  ...over,
});

beforeEach(() => vi.resetAllMocks());

describe("live router", () => {
  it("maps the engine row: schedule choices, camelCase, active", async () => {
    api.get.mockResolvedValue(row());
    const live = await call(liveRouter.live.get, { artifactId });
    expect(live).toMatchObject({
      schedule: { frequency: "weekly", time: "08:00", weekday: 0 },
      active: true,
      followPublish: true,
      onChange: true,
      note: "prices only",
      checkedAt: 100,
      lastUpdate: { suggestions: [{ id: "name", sourceValue: "B", yours: "A" }] },
    });
    api.get.mockResolvedValue(null);
    expect(await call(liveRouter.live.get, { artifactId })).toBeNull();
  });

  it("publishes a held update and passes the held reason through", async () => {
    api.get.mockResolvedValue(row({ held: { version: 3, reason: "unplaced" } }));
    expect(await call(liveRouter.live.get, { artifactId })).toMatchObject({
      held: { version: 3, reason: "unplaced" },
    });
    api.publish.mockResolvedValue(row());
    expect(await call(liveRouter.live.publish, { artifactId })).toMatchObject({ held: null });
    expect(api.publish).toHaveBeenCalledWith("client", "", artifactId);
  });

  it("starts fresh and keeps the note", async () => {
    api.reset.mockResolvedValue(row({ last_update: null }));
    expect(await call(liveRouter.live.reset, { artifactId })).toMatchObject({
      note: "prices only",
      lastUpdate: null,
    });
  });

  it("saves the schedule as an RRULE and only the fields given", async () => {
    api.put.mockResolvedValue(row({ schedule: "paused" }));
    const live = await call(liveRouter.live.save, {
      artifactId,
      schedule: { frequency: "daily", time: "09:30" },
      tell: "every",
    });
    expect(api.put).toHaveBeenCalledWith("client", "", artifactId, {
      rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=30",
      tell: "every",
    });
    expect(live?.active).toBe(false);
  });

  it("stops, runs, undoes and accepts through the engine", async () => {
    api.del.mockResolvedValue(undefined);
    api.run.mockResolvedValue(undefined);
    api.undo.mockResolvedValue({ id: "a2", version: 4 });
    api.accept.mockResolvedValue({ id: "a3", version: 5 });
    expect(await call(liveRouter.live.stop, { artifactId })).toEqual({ ok: true });
    expect(await call(liveRouter.live.run, { artifactId })).toEqual({ ok: true });
    expect(await call(liveRouter.live.undo, { artifactId })).toEqual({ id: "a2", version: 4 });
    expect(await call(liveRouter.live.accept, { artifactId, elementId: "name" })).toEqual({
      id: "a3",
      version: 5,
    });
    expect(api.accept).toHaveBeenCalledWith("client", "", artifactId, "name");
  });
});
