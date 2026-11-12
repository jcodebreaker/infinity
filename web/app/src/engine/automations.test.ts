import { beforeEach, describe, expect, it, vi } from "vitest";

const { listTasks, listSystemJobs, createTask, patchTask, runTask, deleteTask } = vi.hoisted(
  () => ({
    listSystemJobs: vi.fn(),
    listTasks: vi.fn(),
    createTask: vi.fn(),
    patchTask: vi.fn(),
    runTask: vi.fn(),
    deleteTask: vi.fn(),
  }),
);

vi.mock("@superchat/adapters/omnigent/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@superchat/adapters/omnigent/browser")>()),
  listOmnigentScheduledTasks: listTasks,
  listOmnigentSystemJobs: listSystemJobs,
  createOmnigentScheduledTask: createTask,
  patchOmnigentScheduledTask: patchTask,
  runOmnigentScheduledTask: runTask,
  deleteOmnigentScheduledTask: deleteTask,
}));
vi.mock("./client", () => ({
  engineClient: () => "client",
  museSessionId: async () => "s1",
  NO_EMAIL: "",
}));

const { automationsRouter, rruleToSchedule, scheduleToRrule } = await import("./automations");
const { call } = await import("@orpc/server");

const botId = "00000000-0000-4000-8000-000000000001";
const task = (over: Record<string, unknown> = {}) => ({
  id: "t1",
  name: "Morning brief",
  prompt: "Summarise my inbox",
  rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
  timezone: "Europe/Paris",
  state: "active",
  kind: "automation",
  created_at: 1_700_000_000,
  last_run_at: 1_700_003_600,
  last_run_status: "succeeded",
  next_run_at: "2026-01-02T08:00:00+01:00",
  ...over,
});

beforeEach(() => vi.resetAllMocks());

describe("automation schedule", () => {
  it.each([
    [{ frequency: "daily", time: "09:05" } as const, "FREQ=DAILY;BYHOUR=9;BYMINUTE=5"],
    [
      { frequency: "weekly", time: "17:00", weekday: 4 } as const,
      "FREQ=WEEKLY;BYDAY=FR;BYHOUR=17;BYMINUTE=0",
    ],
    [
      { frequency: "monthly", time: "08:30", monthDay: 15 } as const,
      "FREQ=MONTHLY;BYMONTHDAY=15;BYHOUR=8;BYMINUTE=30",
    ],
  ])("round-trips %o", (schedule, rrule) => {
    expect(scheduleToRrule(schedule)).toBe(rrule);
    expect(rruleToSchedule(rrule)).toEqual(schedule);
  });

  it("returns null for rules the form cannot express", () => {
    expect(rruleToSchedule("FREQ=HOURLY")).toBeNull();
    expect(rruleToSchedule("FREQ=WEEKLY;BYDAY=MO,TU;BYHOUR=9;BYMINUTE=0")).toBeNull();
    expect(rruleToSchedule("FREQ=MONTHLY;BYMONTHDAY=31;BYHOUR=9;BYMINUTE=0")).toBeNull();
  });
});

describe("automations router", () => {
  it("lists only automation tasks bound to the Muse's Super Chat", async () => {
    listTasks.mockResolvedValue([task(), task({ id: "t2", kind: "followed_topic" })]);
    const list = (await call(automationsRouter.automations.list, { botId })) as unknown[];
    expect(listTasks).toHaveBeenCalledWith("client", "", "s1");
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({
      id: "t1",
      enabled: true,
      schedule: { frequency: "daily", time: "08:00" },
      lastRunStatus: "succeeded",
      nextRunAt: "2026-01-02T08:00:00+01:00",
    });
  });

  it("maps the engine's system jobs", async () => {
    listSystemJobs.mockResolvedValue([
      {
        id: "memory_upkeep",
        name: "Memory upkeep",
        description: "Keeps your memory fresh",
        schedule: "Every hour",
        last_run_at: 1_700_000_000,
        next_run_at: null,
        enabled: true,
      },
    ]);
    expect(await call(automationsRouter.automations.system, { botId })).toEqual([
      {
        id: "memory_upkeep",
        name: "Memory upkeep",
        description: "Keeps your memory fresh",
        schedule: "Every hour",
        lastRunAt: "2023-11-14T22:13:20.000Z",
        nextRunAt: null,
        enabled: true,
      },
    ]);
  });

  it("creates a Helper task marked automation with the rule built from the choices", async () => {
    createTask.mockResolvedValue(task({ model_override: "claude-haiku-4-5" }));
    const created = await call(automationsRouter.automations.create, {
      botId,
      name: "Morning brief",
      prompt: "Summarise my inbox",
      schedule: { frequency: "daily", time: "08:00" },
      timezone: "Europe/Paris",
      model: "claude-haiku-4-5",
    });
    expect(createTask).toHaveBeenCalledWith("client", "", {
      name: "Morning brief",
      prompt: "Summarise my inbox",
      rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
      parentSessionId: "s1",
      agentType: "worker",
      kind: "automation",
      timezone: "Europe/Paris",
      modelOverride: "claude-haiku-4-5",
    });
    expect(created).toMatchObject({ model: "claude-haiku-4-5" });
  });

  it("pauses, runs and removes only automations of this Muse", async () => {
    listTasks.mockResolvedValue([task(), task({ id: "other", kind: "followed_topic" })]);
    patchTask.mockResolvedValue(task({ state: "paused" }));
    const paused = await call(automationsRouter.automations.update, {
      botId,
      id: "t1",
      enabled: false,
    });
    expect(patchTask).toHaveBeenCalledWith("client", "", "t1", { state: "paused" });
    expect(paused).toMatchObject({ enabled: false, nextRunAt: null });

    await call(automationsRouter.automations.run, { botId, id: "t1" });
    expect(runTask).toHaveBeenCalledWith("client", "", "t1");
    await call(automationsRouter.automations.remove, { botId, id: "t1" });
    expect(deleteTask).toHaveBeenCalledWith("client", "", "t1");

    await expect(
      call(automationsRouter.automations.remove, { botId, id: "other" }),
    ).rejects.toMatchObject({
      code: "NOT_FOUND",
    });
    expect(deleteTask).toHaveBeenCalledTimes(1);
  });
});
