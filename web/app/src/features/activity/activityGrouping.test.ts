import { afterEach, beforeEach, expect, it } from "vitest";
import {
  ACTIVITY_POLL_ACTIVE_MS,
  ACTIVITY_POLL_IDLE_MS,
  ACTIVITY_POLL_IDLE_WATCHED_MS,
  activitiesPollIntervalMs,
  activityDateLabel,
  activityDurationMs,
  activityLineText,
  formatClockTime,
  formatDayAndTime,
  formatDuration,
  groupActivitiesByDate,
  groupStudioTurns,
  isHelperActivity,
  mergeNewestPage,
} from "./activityGrouping";
import { activity } from "./activityTestKit";

// The day-grouping helpers read the local calendar day off `Date` getters, which follow
// `process.env.TZ`. Pin it to UTC by default so every other test here is deterministic
// regardless of the machine running it; the one test that cares about a different zone
// sets its own and relies on this file's `afterEach` to restore it.
let originalTZ: string | undefined;
beforeEach(() => {
  originalTZ = process.env.TZ;
  process.env.TZ = "UTC";
});
afterEach(() => {
  process.env.TZ = originalTZ;
});

const LABELS = {
  in_progress: "Working",
  done: "Done",
  failed: "Didn't finish",
  cancelled: "Cancelled",
};

it("polls briskly while anything is in progress, and relaxed once everything has settled", () => {
  expect(activitiesPollIntervalMs([])).toBe(ACTIVITY_POLL_IDLE_MS);
  expect(activitiesPollIntervalMs([activity({ status: "done" })])).toBe(ACTIVITY_POLL_IDLE_MS);
  expect(
    activitiesPollIntervalMs([activity({ status: "done" }), activity({ status: "in_progress" })]),
  ).toBe(ACTIVITY_POLL_ACTIVE_MS);
});

it("relaxes further while the engine's live signal is connected, but never while running", () => {
  expect(activitiesPollIntervalMs([], true)).toBe(ACTIVITY_POLL_IDLE_WATCHED_MS);
  expect(activitiesPollIntervalMs([activity({ status: "in_progress" })], true)).toBe(
    ACTIVITY_POLL_ACTIVE_MS,
  );
});

it("says a span the way a person would", () => {
  expect(formatDuration(0)).toBe("0s");
  expect(formatDuration(12_900)).toBe("12s");
  expect(formatDuration(80_000)).toBe("1m 20s");
  expect(formatDuration(3_900_000)).toBe("1h 5m");
  expect(formatDuration(-5)).toBe("0s");
});

it("times a running Activity against now and a settled one by its own finish", () => {
  const running = activity({ status: "in_progress", finishedAt: null });
  const now = Date.parse(running.startedAt) + 80_000;
  expect(activityDurationMs(running, now)).toBe(80_000);
  expect(activityDurationMs(activity(), now)).toBe(60_000);
  expect(activityDurationMs(activity({ finishedAt: null }), now)).toBeNull();
});

it("marks a Helper's whole task as kind sub_agent, and a plain turn as not one", () => {
  expect(isHelperActivity(activity({ kind: "sub_agent" }))).toBe(true);
  expect(isHelperActivity(activity({ kind: "turn" }))).toBe(false);
  // A studio turn is never a Helper row, however the engine stores it (ADR 0024).
  expect(isHelperActivity(activity({ kind: "sub_agent", source: "studio" }))).toBe(false);
});

it("gathers a file's studio turns under its name, where its newest turn is", () => {
  const deck = { groupId: "g-deck", name: "q3_review.deck.html" };
  const entries = groupStudioTurns([
    activity({ id: "s3", source: "studio", artifact: deck, title: "Biggest first" }),
    activity({ id: "t1", title: "Checked the budget" }),
    activity({ id: "p1", source: "studio", artifact: { groupId: "g-page", name: "site.html" } }),
    activity({ id: "s1", source: "studio", artifact: deck, title: "Bar chart" }),
    // A studio turn that names no file stays a row of its own.
    activity({ id: "s0", source: "studio" }),
  ]);
  expect(
    entries.map((entry) =>
      entry.kind === "file"
        ? `${entry.name}: ${entry.activities.map((a) => a.id).join(",")}`
        : entry.activity.id,
    ),
  ).toEqual(["q3_review.deck.html: s3,s1", "t1", "site.html: p1", "s0"]);
});

it("shows the summary, else the outcome, when there is one", () => {
  expect(
    activityLineText(
      { status: "done", summary: "Ties out.", outcome: "Ties out. Every line matches." },
      LABELS,
    ),
  ).toBe("Ties out.");
  expect(activityLineText({ status: "done", summary: null, outcome: "Ties out." }, LABELS)).toBe(
    "Ties out.",
  );
});

it("strips markdown from the outcome line", () => {
  expect(
    activityLineText(
      { status: "done", summary: null, outcome: "## AI Agent Launches\n\n**Major launches:**" },
      LABELS,
    ),
  ).toBe("AI Agent Launches Major launches:");
});

it("falls back to a plain status word when there is no outcome yet", () => {
  expect(activityLineText({ status: "in_progress", summary: null, outcome: null }, LABELS)).toBe(
    "Working",
  );
  expect(activityLineText({ status: "failed", summary: null, outcome: null }, LABELS)).toBe(
    "Didn't finish",
  );
  expect(activityLineText({ status: "cancelled", summary: null, outcome: null }, LABELS)).toBe(
    "Cancelled",
  );
});

it("formats a clock time in the viewer's zone, and the day for the run page", () => {
  expect(formatClockTime("2026-10-03T11:21:00.000Z", "en")).toBe("11:21 AM");
  expect(formatClockTime("not a date")).toBe("");
  expect(formatDayAndTime("2026-10-03T14:05:00.000Z", "en")).toBe("Oct 3, 2:05 PM");
});

it("labels today and yesterday, and a weekday+date for anything older", () => {
  const now = new Date("2026-10-03T15:00:00.000Z");
  expect(activityDateLabel("2026-10-03", now)).toEqual({ kind: "today" });
  expect(activityDateLabel("2026-10-02", now)).toEqual({ kind: "yesterday" });
  const older = activityDateLabel("2026-09-28", now, "en");
  expect(older.kind).toBe("weekday");
  if (older.kind === "weekday") {
    expect(older.text).toContain("Sep");
    expect(older.text).toContain("28");
  }
});

it("groups a newest-first list by day, keeping order, without re-sorting", () => {
  const now = new Date("2026-10-03T15:00:00.000Z");
  const activities = [
    activity({ id: "a1", startedAt: "2026-10-03T14:00:00.000Z" }),
    activity({ id: "a2", startedAt: "2026-10-03T09:00:00.000Z" }),
    activity({ id: "a3", startedAt: "2026-10-02T20:00:00.000Z" }),
    activity({ id: "a4", startedAt: "2026-09-20T12:00:00.000Z" }),
  ];
  const groups = groupActivitiesByDate(activities, now);
  expect(groups).toHaveLength(3);
  expect(groups[0]?.label).toEqual({ kind: "today" });
  expect(groups[0]?.activities.map((a) => a.id)).toEqual(["a1", "a2"]);
  expect(groups[1]?.label).toEqual({ kind: "yesterday" });
  expect(groups[1]?.activities.map((a) => a.id)).toEqual(["a3"]);
  expect(groups[2]?.label.kind).toBe("weekday");
  expect(groups[2]?.activities.map((a) => a.id)).toEqual(["a4"]);
});

it("groups by the viewer's local calendar day, ignoring the engine's own `date` field", () => {
  // The engine's `date` says "2026-10-04"; the instant is still "2026-10-03" evening in
  // New York (UTC-4 in October) — the viewer's own calendar day, which is what the panel
  // must group by, not the server's.
  process.env.TZ = "America/New_York";
  const now = new Date("2026-10-03T12:00:00.000Z"); // 8am in New York, Oct 3
  const activities = [
    activity({ id: "a1", date: "2026-10-04", startedAt: "2026-10-03T23:30:00.000Z" }),
  ];
  const groups = groupActivitiesByDate(activities, now);
  expect(groups).toHaveLength(1);
  expect(groups[0]?.date).toBe("2026-10-03");
  expect(groups[0]?.label).toEqual({ kind: "today" });
});

it("returns nothing for an empty list", () => {
  expect(groupActivitiesByDate([], new Date())).toEqual([]);
});

it("merges a freshly-polled newest page into the already-loaded list without losing earlier pages", () => {
  const loadedViaOlder = activity({ id: "old-1", title: "Set up the weekly KPI routine" });
  const existing = [
    activity({ id: "a1", status: "in_progress", outcome: "Comparing line 40 of 84" }),
    activity({ id: "a2", status: "done" }),
    loadedViaOlder,
  ];
  // The newest poll: "a1" finished since the last poll, "a2" is unchanged, and "a3" is
  // genuinely new (wasn't in the list before).
  const newest = [
    activity({ id: "a3", title: "Brand new turn" }),
    activity({ id: "a1", status: "done", outcome: "Everything ties out." }),
    activity({ id: "a2", status: "done" }),
  ];
  const merged = mergeNewestPage(existing, newest);
  expect(merged.map((a) => a.id)).toEqual(["a3", "a1", "a2", "old-1"]);
  // "a1" is updated in place (same position as before), not moved to the front.
  expect(merged[1]).toEqual(newest[1]);
  // The page loaded via "Load earlier" survives untouched.
  expect(merged.at(-1)).toEqual(loadedViaOlder);
});

it("leaves the list untouched when the newest page has nothing new or changed", () => {
  const existing = [activity({ id: "a1" }), activity({ id: "a2" })];
  const merged = mergeNewestPage(existing, [activity({ id: "a1" })]);
  expect(merged.map((a) => a.id)).toEqual(["a1", "a2"]);
});

it("lists a turn copied into a side chat once", () => {
  const now = new Date("2026-05-10T12:00:00");
  const groups = groupActivitiesByDate(
    [
      activity({ id: "turn:chat_root:resp_1", chatId: "chat_root" }),
      activity({ id: "turn:chat_side:resp_1", chatId: "chat_side" }),
      activity({ id: "turn:chat_side:resp_2", chatId: "chat_side" }),
    ],
    now,
  );
  expect(groups.flatMap((group) => group.activities.map((a) => a.id))).toEqual([
    "turn:chat_root:resp_1",
    "turn:chat_side:resp_2",
  ]);
});
