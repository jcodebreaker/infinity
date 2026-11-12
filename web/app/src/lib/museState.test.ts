import type { MessageBlock } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { activityVerb, currentToolName, deriveMuseState, museActivityLabel } from "./museState";

describe("deriveMuseState", () => {
  it("is idle with no runs and nothing waiting", () => {
    expect(deriveMuseState([], 0)).toBe("idle");
  });

  it("is thinking while a run is queued or leased", () => {
    expect(deriveMuseState([{ status: "queued" }], 0)).toBe("thinking");
    expect(deriveMuseState([{ status: "leased" }], 0)).toBe("thinking");
  });

  it("is working while a run is running (or paused waiting on input)", () => {
    expect(deriveMuseState([{ status: "running" }], 0)).toBe("working");
    expect(deriveMuseState([{ status: "waiting_input" }], 0)).toBe("working");
  });

  it("prefers the most alive status across concurrent runs", () => {
    expect(deriveMuseState([{ status: "queued" }, { status: "running" }], 0)).toBe("working");
    expect(deriveMuseState([{ status: "completed" }, { status: "leased" }], 0)).toBe("thinking");
  });

  it("waiting wins over any active run, and applies even with no runs", () => {
    expect(deriveMuseState([{ status: "running" }], 2)).toBe("waiting");
    expect(deriveMuseState([], 1)).toBe("waiting");
  });
});

describe("currentToolName", () => {
  it("is undefined with no blocks", () => {
    expect(currentToolName(undefined)).toBeUndefined();
    expect(currentToolName([])).toBeUndefined();
  });

  it("reads the last pending tool name off a live progress block", () => {
    const blocks: MessageBlock[] = [
      { kind: "progress", text: "", activity: true, pendingToolNames: ["read_file", "write_file"] },
    ];
    expect(currentToolName(blocks)).toBe("write_file");
  });

  it("reads the last step's label once tool calls have flushed to steps", () => {
    const blocks: MessageBlock[] = [
      {
        kind: "steps",
        steps: [
          { label: "Read file", count: 1 },
          { label: "Write file", count: 2 },
        ],
      },
    ];
    expect(currentToolName(blocks)).toBe("Write file");
  });

  it("is undefined for a plain text tail with no tool activity", () => {
    const blocks: MessageBlock[] = [{ kind: "text", text: "Here's what I found." }];
    expect(currentToolName(blocks)).toBeUndefined();
  });
});

describe("activityVerb", () => {
  it("maps known tool names, raw or humanized, to a short verb", () => {
    expect(activityVerb("write_file")).toBe("Writing a file…");
    expect(activityVerb("Write file")).toBe("Writing a file…");
    expect(activityVerb("shell")).toBe("Running code…");
    expect(activityVerb("browser_navigate")).toBe("Browsing…");
    expect(activityVerb("browser_snapshot")).toBe("Browsing…");
    expect(activityVerb("web_search")).toBe("Searching the web…");
    expect(activityVerb("run_subagent")).toBe("Running a task…");
  });

  it("falls back to a generic verb for unknown or missing tools", () => {
    expect(activityVerb("some_new_tool")).toBe("Working…");
    expect(activityVerb(undefined)).toBe("Working…");
    expect(activityVerb(null)).toBe("Working…");
  });
});

describe("museActivityLabel", () => {
  it("is undefined while idle", () => {
    expect(museActivityLabel("idle")).toBeUndefined();
  });

  it("is 'Needs you' while waiting, regardless of any tool name", () => {
    expect(museActivityLabel("waiting", "shell")).toBe("Needs you");
  });

  it("is 'Thinking…' while thinking", () => {
    expect(museActivityLabel("thinking")).toBe("Thinking…");
  });

  it("is the mapped verb while working", () => {
    expect(museActivityLabel("working", "write_file")).toBe("Writing a file…");
    expect(museActivityLabel("working")).toBe("Working…");
  });
});
