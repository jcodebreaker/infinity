import type { Activity } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { helper } from "./activityTestKit";
import { workingSince } from "./superchatWork";

const START = "2026-10-07T10:00:00.000Z";
function activity(
  id: string,
  status: Activity["status"],
  source: Activity["source"] = "background",
): Activity {
  return helper(id, { status, source, startedAt: START, finishedAt: null });
}

describe("workingSince", () => {
  const helperStart = Date.parse("2026-10-07T10:00:00.000Z");

  it("counts from the earliest of the turn and the running Helpers", () => {
    expect(workingSince(null, [])).toBeNull();
    expect(workingSince(null, [activity("h", "in_progress")])).toBe(helperStart);
    expect(workingSince(helperStart + 9_000, [activity("h", "in_progress")])).toBe(helperStart);
    expect(workingSince(helperStart + 9_000, [activity("h", "done")])).toBe(helperStart + 9_000);
  });

  it("never counts a file's studio turn as the Conversation's work", () => {
    expect(workingSince(null, [activity("s", "in_progress", "studio")])).toBeNull();
  });
});
