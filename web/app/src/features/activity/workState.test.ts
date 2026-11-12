import type { RoundSummary } from "@superchat/contracts";
import { expect, it } from "vitest";
import { helper } from "./activityTestKit";
import type { TurnStatus } from "./workState";
import { deriveWorkState, IDLE_TURN } from "./workState";

const START = Date.parse("2026-10-17T10:00:00.000Z");
const turnAt = START + 30_000;
const running = (overrides: Partial<TurnStatus> = {}) => ({ ...IDLE_TURN, ...overrides });
const derive = (
  turn: TurnStatus,
  {
    sending = false,
    stopping = false,
    activities = [] as ReturnType<typeof helper>[],
    rounds = [] as RoundSummary[],
    roundId = undefined as string | undefined,
  } = {},
) =>
  deriveWorkState({ turn, sending, stopping, activities, turnStartedAt: turnAt, rounds, roundId });
const helperRunning = helper("h1", {
  status: "in_progress",
  startedAt: new Date(START).toISOString(),
  finishedAt: null,
});
const round = (roundId: string, status: string, startedAt = START): RoundSummary => ({
  roundId,
  status,
  startedAt,
  settledAt: null,
  deliveredAt: null,
  live: null,
  helpers: 0,
  files: 0,
  steps: 0,
});

it("is idle with nothing running, and counts nothing", () => {
  expect(derive(IDLE_TURN)).toEqual({
    phase: "idle",
    running: false,
    turnRunning: false,
    since: null,
    cancelled: false,
    paused: false,
  });
});

it("runs while the newest Round runs, from its start, whatever the transcript says", () => {
  const rounds = [round("m2", "running", START), round("m1", "done", START - 90_000)];
  expect(derive(IDLE_TURN, { rounds, roundId: "m2" })).toMatchObject({
    phase: "running",
    turnRunning: true,
    since: START,
  });
  // Settled: idle, though the transcript has not heard the turn end.
  const settled = [round("m2", "done"), round("m1", "done")];
  expect(derive(running({ running: true }), { rounds: settled, roundId: "m2" }).phase).toBe("idle");
});

it("never lights for a Helper outside the Round", () => {
  const rounds = [round("m1", "done")];
  expect(derive(IDLE_TURN, { rounds, roundId: "m1", activities: [helperRunning] }).running).toBe(
    false,
  );
});

it("runs on the transcript's word for a message whose Round has not arrived yet", () => {
  const rounds = [round("m1", "done")];
  expect(derive(running({ awaiting: true }), { rounds })).toMatchObject({
    phase: "running",
    since: null,
  });
  expect(derive(running({ awaiting: true, stopped: true }), { rounds }).running).toBe(false);
});

it("before Rounds, runs while the turn or a Helper runs, from the earliest start", () => {
  expect(derive(running({ running: true }))).toMatchObject({ phase: "running", since: turnAt });
  expect(derive(running({ awaiting: true })).since).toBe(turnAt);
  expect(derive(IDLE_TURN, { activities: [helperRunning] })).toMatchObject({
    phase: "running",
    turnRunning: false,
    since: START,
  });
  const studio = helper("s1", { ...helperRunning, source: "studio" });
  expect(derive(IDLE_TURN, { activities: [studio] }).running).toBe(false);
});

it("says sending, then stopping, over whatever runs", () => {
  expect(derive(IDLE_TURN, { sending: true })).toMatchObject({
    phase: "sending",
    running: true,
    turnRunning: true,
  });
  expect(
    derive(IDLE_TURN, { sending: true, stopping: true, rounds: [round("m1", "running")] }).phase,
  ).toBe("stopping");
});

it("before Rounds, stops at once when a Stop landed, though the turn and its Helpers are ending", () => {
  const stopped = derive(running({ running: true, stopped: true, cancelled: true }), {
    activities: [helperRunning],
  });
  expect(stopped).toEqual({
    phase: "idle",
    running: false,
    turnRunning: false,
    since: null,
    cancelled: true,
    paused: false,
  });
});

it("is paused only while Pause holds every running Helper of the running Round", () => {
  const rounds = [round("m1", "running")];
  const held = { ...helperRunning, paused: true, roundId: "m1" };
  const other = helper("h2", { ...helperRunning, chatId: "h2", roundId: "m1" });
  expect(derive(IDLE_TURN, { rounds, roundId: "m1", activities: [held] })).toMatchObject({
    running: true,
    paused: true,
  });
  expect(derive(IDLE_TURN, { rounds, roundId: "m1", activities: [held, other] }).paused).toBe(
    false,
  );
  // A held Helper outside the Round says nothing of it.
  expect(
    derive(IDLE_TURN, { rounds, roundId: "m1", activities: [{ ...held, roundId: "m0" }] }).paused,
  ).toBe(false);
  expect(derive(IDLE_TURN, { rounds, roundId: "m1" }).paused).toBe(false);
});
