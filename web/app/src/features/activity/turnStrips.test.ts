import { expect, it } from "vitest";
import type { TurnStrips } from "./turnStrips";
import { nextTurnStrips } from "./turnStrips";
import type { TranscriptWork, TurnWork } from "./workPlan";
import type { WorkState } from "./workState";

const turn = (anchorId: string, text: string, last = false, helpers = 0): TurnWork => ({
  anchorId,
  activityId: null,
  // What the reply's turn saved: a different file is different work.
  files: [text],
  helpers: Array.from({ length: helpers }, (_, i) => ({ helperId: `h${i}`, title: "Helper" })),
  startedAt: 1_000,
  endedAt: 2_000,
  last,
  outcome: "done",
});

const work = (turns: TurnWork[], drawn: string[] = []): TranscriptWork => ({
  turns: new Map(turns.map((found) => [found.anchorId, found])),
  drawn: new Set(drawn),
});

const state = (over: Partial<WorkState> = {}): WorkState => ({
  phase: "idle",
  running: false,
  turnRunning: false,
  since: null,
  cancelled: false,
  paused: false,
  ...over,
});
const RUNNING = state({ phase: "running", running: true, turnRunning: true, since: 1_000 });

const NONE: TurnStrips = { strips: new Map(), drawn: new Set(), keys: new Map() };

it("keeps a strip's object while its reply's work did not change", () => {
  const first = nextTurnStrips(
    NONE,
    work([turn("a", "Read"), turn("b", "Write", true)], ["a:0"]),
    state(),
    undefined,
  );
  // Rebuilt from the transcript: new objects, the same work for "a"; "b"'s work moved on.
  const second = nextTurnStrips(
    first,
    work([turn("a", "Read"), turn("b", "Check", true)], ["a:0"]),
    state(),
    undefined,
  );
  expect(second.strips.get("a")).toBe(first.strips.get("a"));
  expect(second.strips.get("b")).not.toBe(first.strips.get("b"));
  expect(second.drawn).toBe(first.drawn);
  // The newest reply starts running: only its strip is live.
  const third = nextTurnStrips(
    second,
    work([turn("a", "Read"), turn("b", "Check", true)], ["a:0"]),
    RUNNING,
    "Reading files…",
  );
  expect(third.strips.get("a")).toBe(second.strips.get("a"));
  expect(third.strips.get("b")?.turn).toMatchObject({ live: true, liveLabel: "Reading files…" });
  // Nothing new for either: both kept.
  const fourth = nextTurnStrips(
    third,
    work([turn("a", "Read"), turn("b", "Check", true)], ["a:0"]),
    RUNNING,
    "Reading files…",
  );
  expect(fourth.strips.get("b")).toBe(third.strips.get("b"));
});

it("lights a strip with Helpers while anything runs, and reads a stopped newest turn as cancelled", () => {
  const found = work([turn("a", "Read", false, 1), turn("b", "Write", true)]);
  const running = nextTurnStrips(
    NONE,
    found,
    state({ phase: "running", running: true, since: 1 }),
    undefined,
  );
  expect(running.strips.get("a")?.lit).toBe(true);
  expect(running.strips.get("b")?.turn.live).toBe(false);
  const stopped = nextTurnStrips(running, found, state({ cancelled: true }), undefined);
  expect(stopped.strips.get("a")?.lit).toBe(false);
  expect(stopped.strips.get("a")?.turn.outcome).toBe("done");
  expect(stopped.strips.get("b")?.turn.outcome).toBe("cancelled");
});
