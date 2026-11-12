import type { ArtifactVersion, AutomationRun, LivePage } from "@superchat/contracts";
import { expect, it } from "vitest";
import { liveStatus, liveTrigger, versionsByRun, watchedFiles } from "./liveRuns";

const iso = (seconds: number) => new Date(seconds * 1000).toISOString();
const run = (id: string, fired: number, finished: number | null, status = "succeeded") =>
  ({
    id,
    status,
    scheduledAt: iso(fired),
    firedAt: iso(fired),
    finishedAt: finished && iso(finished),
    errorCode: null,
    chatId: `chat-${id}`,
  }) as AutomationRun;
const version = (id: string, at: number, origin = "update") =>
  ({ id, version: 2, name: "p.html", createdAt: iso(at), origin }) as ArtifactVersion;

const page = (over: Partial<LivePage> = {}): LivePage => ({
  artifactId: "a1",
  name: "p.html",
  title: "Prices",
  taskId: "t1",
  schedule: { frequency: "weekly", time: "08:00", weekday: 0 },
  onChange: true,
  sources: ["/home/me/report.csv", "https://example.com/x"],
  checkedAt: 1000,
  failures: 0,
  lastFailure: null,
  paused: false,
  openIssue: null,
  lastUpdate: null,
  ...over,
});

it("matches each update version to the run that made it, and ignores hand edits", () => {
  const runs = [run("r2", 2000, 2060), run("r1", 1000, 1050)];
  const made = versionsByRun(runs, [
    version("v3", 2040),
    version("v2", 1030),
    version("v1", 1500, "manual"),
  ]);
  expect(made.get("r2")?.id).toBe("v3");
  expect(made.get("r1")?.id).toBe("v2");
  expect(made.size).toBe(2);
});

it("links a version to the run whose chat saved it, and falls back to time for older ones", () => {
  const runs = [run("r2", 2000, 2060), run("r1", 1000, 1050)];
  // Saved by r1's chat, though its time would match r2.
  const linked = { ...version("v3", 2040), chatId: "chat-r1" } as ArtifactVersion;
  const made = versionsByRun(runs, [linked, version("v2", 2030)]);
  expect(made.get("r1")?.id).toBe("v3");
  expect(made.get("r2")?.id).toBe("v2");
});

it("does not give a version to a run that ended long before it", () => {
  expect(versionsByRun([run("r1", 1000, 1050)], [version("v2", 5000)]).size).toBe(0);
});

it("names the files the page follows, and the trigger line", () => {
  expect(watchedFiles(page())).toEqual(["report.csv"]);
  expect(watchedFiles(page({ onChange: false }))).toEqual([]);
  const when = (files: string) => `When ${files} changes`;
  expect(liveTrigger(page(), { when })).toBe("When report.csv changes · Every Monday at 08:00");
  expect(liveTrigger(page({ sources: ["https://example.com"] }), { when })).toBe(
    "Every Monday at 08:00",
  );
});

it("reads the last run: paused, failed, changed, checked", () => {
  expect(liveStatus(page({ paused: true, failures: 3 }))).toEqual({ kind: "paused", failures: 3 });
  expect(liveStatus(page({ failures: 1, lastFailure: { at: 5, reason: "no source" } }))).toEqual({
    kind: "failed",
    reason: "no source",
  });
  expect(liveStatus(page({ lastUpdate: { version: 2, at: 990, changes: 1 } }))).toEqual({
    kind: "changed",
    changes: 1,
    at: 990,
  });
  expect(liveStatus(page({ lastUpdate: { version: 2, at: 100, changes: 1 } }))).toEqual({
    kind: "checked",
    at: 1000,
  });
  expect(liveStatus(page({ checkedAt: null }))).toEqual({ kind: "never" });
});
