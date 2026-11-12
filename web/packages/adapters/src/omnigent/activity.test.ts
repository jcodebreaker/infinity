import { describe, expect, it } from "vitest";
import { mapActivity, mapActivityStep, plainFailureOutcome } from "./activity.js";

describe("mapActivityStep", () => {
  it("maps snake_case fields to camelCase and defaults a missing tool/detail", () => {
    expect(
      mapActivityStep({ item_id: "fc_1", title: "Searched memory for 'Q4'", created_at: 1000 }),
    ).toEqual({
      itemId: "fc_1",
      title: "Searched memory for 'Q4'",
      createdAt: new Date(1000 * 1000).toISOString(),
      tool: null,
      detail: undefined,
    });
  });

  it("carries tool and detail through when present", () => {
    const mapped = mapActivityStep({
      item_id: "fc_2",
      title: "Used memory_search",
      created_at: 2000,
      tool: "memory_search",
      detail: { query: "Q4" },
    });
    expect(mapped.tool).toBe("memory_search");
    expect(mapped.detail).toEqual({ query: "Q4" });
  });
});

describe("mapActivity", () => {
  it("maps a turn Activity, defaulting outcome/finishedAt to null and steps to undefined", () => {
    expect(
      mapActivity({
        id: "turn:conv_abc:resp_xyz",
        kind: "turn",
        source: "turn",
        chat_id: "conv_abc",
        title: "Check Q4 totals",
        outcome: null,
        summary: null,
        status: "in_progress",
        started_at: 1000,
        finished_at: null,
        date: "2026-10-03",
      }),
    ).toEqual({
      id: "turn:conv_abc:resp_xyz",
      kind: "turn",
      chatId: "conv_abc",
      source: "turn",
      title: "Check Q4 totals",
      outcome: null,
      summary: null,
      status: "in_progress",
      startedAt: new Date(1000 * 1000).toISOString(),
      finishedAt: null,
      date: "2026-10-03",
      parentChatId: null,
      steps: undefined,
      summaryLine: null,
      families: [],
    });
  });

  it("maps the engine's summary line and families, dropping a family it does not know", () => {
    const mapped = mapActivity({
      id: "turn:conv_abc:resp_1",
      kind: "turn",
      source: "turn",
      chat_id: "conv_abc",
      title: "Build the Acme deck",
      outcome: null,
      summary: null,
      status: "done",
      started_at: 1000,
      finished_at: 1060,
      date: "2026-10-03",
      summary_line: "Created 2 files, searched the web, and 12 more steps",
      families: ["web", "decks", "telepathy", "files"],
    });
    expect(mapped.summaryLine).toBe("Created 2 files, searched the web, and 12 more steps");
    expect(mapped.families).toEqual(["web", "decks", "files"]);
  });

  it("marks a Helper held by Pause and the Round it is in, and nothing for any other value", () => {
    const raw = {
      id: "sub_agent:h1",
      kind: "sub_agent" as const,
      source: "background" as const,
      chat_id: "h1",
      title: "Build the deck",
      outcome: null,
      summary: null,
      status: "in_progress" as const,
      started_at: 1000,
      finished_at: null,
      date: "2026-10-03",
    };
    expect(mapActivity({ ...raw, paused: true }).paused).toBe(true);
    expect(mapActivity({ ...raw, paused: "yes" })).not.toHaveProperty("paused");
    expect(mapActivity({ ...raw, paused: false })).not.toHaveProperty("paused");
    expect(mapActivity({ ...raw, round_id: "m1" }).roundId).toBe("m1");
    expect(mapActivity({ ...raw, round_id: null })).not.toHaveProperty("roundId");
    expect(mapActivity(raw)).not.toHaveProperty("roundId");
  });

  it("maps a studio turn's file (ADR 0024), and none when the engine sends none", () => {
    const raw = {
      id: "turn:studio_1:resp_1",
      kind: "turn" as const,
      source: "studio" as const,
      chat_id: "studio_1",
      title: "Make the chart a bar chart",
      outcome: null,
      summary: null,
      status: "done" as const,
      started_at: 1000,
      finished_at: 1010,
      date: "2026-10-03",
    };
    const mapped = mapActivity({
      ...raw,
      artifact: { group_id: "group_1", name: "q3_review.deck.html" },
    });
    expect(mapped.source).toBe("studio");
    expect(mapped.artifact).toEqual({ groupId: "group_1", name: "q3_review.deck.html" });
    expect(mapActivity(raw).artifact).toBeUndefined();
    expect(mapActivity({ ...raw, artifact: null }).artifact).toBeUndefined();
  });

  it("maps a finished sub_agent Activity with its Steps", () => {
    const mapped = mapActivity({
      id: "sub_agent:conv_child",
      kind: "sub_agent",
      source: "scheduled",
      chat_id: "conv_child",
      title: "Research Q4 totals",
      outcome: "Found the figures in the Q4 report.",
      summary: "Found the figures in the Q4 report.",
      status: "done",
      started_at: 1000,
      finished_at: 1050,
      date: "2026-10-03",
      steps: [
        {
          item_id: "fc_1",
          title: "Searched memory for 'Q4'",
          created_at: 1010,
          tool: "memory_search",
        },
      ],
    });
    expect(mapped.status).toBe("done");
    expect(mapped.source).toBe("scheduled");
    expect(mapped.summary).toBe("Found the figures in the Q4 report.");
    expect(mapped.outcome).toBe("Found the figures in the Q4 report.");
    expect(mapped.finishedAt).toBe(new Date(1050 * 1000).toISOString());
    expect(mapped.steps).toEqual([
      {
        itemId: "fc_1",
        title: "Searched memory for 'Q4'",
        createdAt: new Date(1010 * 1000).toISOString(),
        tool: "memory_search",
        detail: undefined,
      },
    ]);
  });
});

describe("plainFailureOutcome", () => {
  it("names a known failure in plain words and never leaks internals", () => {
    expect(
      plainFailureOutcome(
        "inner executor error: There's an issue with the selected model (a-model). It may not exist or you may not have access to it.",
      ),
    ).toBe("The model isn't available.");
    expect(plainFailureOutcome("API Error: 529 overloaded_error")).toBe("The model was busy.");
    expect(
      plainFailureOutcome(
        "Claude SDK connect failed: exit code 1\nCLI stderr: /Users/x/.venv/bin/python",
      ),
    ).toBe("Couldn't get started.");
    expect(plainFailureOutcome('Traceback (most recent call last):\n  File "/srv/a.py"')).toBe(
      "Could not complete the task",
    );
  });

  it("keeps a short plain reason and the generic line, and says nothing for none", () => {
    expect(plainFailureOutcome("The spreadsheet had no Q4 tab")).toBe(
      "The spreadsheet had no Q4 tab",
    );
    expect(plainFailureOutcome("Could not complete the task")).toBe("Could not complete the task");
    expect(plainFailureOutcome(null)).toBeNull();
  });

  it("only rewrites failed Activities", () => {
    const base = {
      id: "turn:c:r",
      kind: "turn" as const,
      source: "turn" as const,
      chat_id: "c",
      title: "t",
      summary: null,
      started_at: 1,
      finished_at: 2,
      date: "2026-10-04",
    };
    expect(mapActivity({ ...base, status: "done", outcome: "API Error: 529" }).outcome).toBe(
      "API Error: 529",
    );
    expect(
      mapActivity({ ...base, status: "failed", outcome: "API Error: 529 overloaded_error" }),
    ).toMatchObject({
      outcome: "The model was busy.",
      summary: "The model was busy.",
    });
  });
});

describe("mapActivity nesting", () => {
  const helper = {
    id: "sub_agent:conv_part",
    kind: "sub_agent" as const,
    source: "background" as const,
    chat_id: "conv_part",
    title: "Flights",
    outcome: null,
    summary: null,
    status: "in_progress" as const,
    started_at: 1000,
    finished_at: null,
    date: "2026-10-03",
  };

  it("carries the chat that started a Helper, so a part nests under its coordinator", () => {
    expect(mapActivity({ ...helper, parent_chat_id: "conv_lead" }).parentChatId).toBe("conv_lead");
  });

  it("reads an engine that does not say as no parent", () => {
    expect(mapActivity(helper).parentChatId).toBeNull();
  });
});
