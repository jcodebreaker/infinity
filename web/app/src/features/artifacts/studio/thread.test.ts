// The studio thread (ADR 0024): the studio session's transcript merged with the file's versions.

import type { ArtifactVersion, MessageBlock, ThreadMessage } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { buildElementAsk } from "../editor/html-ask";
import { studioThread, versionOrigin } from "./thread";

const STUDIO = "studio-1";
const CONVERSATION = "conv-1";
const at = (seconds: number) => new Date(seconds * 1000).toISOString();

const message = (
  id: string,
  role: ThreadMessage["role"],
  text: string,
  t: number,
): ThreadMessage => ({
  id,
  threadId: STUDIO,
  seq: t,
  role,
  blocks: [{ kind: "text", text }],
  createdAt: at(t),
});

const notice = (
  id: string,
  t: number,
  block: Omit<Extract<MessageBlock, { kind: "notice" }>, "kind">,
): ThreadMessage => ({
  id,
  threadId: STUDIO,
  seq: t,
  role: "system",
  blocks: [{ kind: "notice", ...block }],
  createdAt: at(t),
});

const handEdit = (n: number, t: number, elementId: string, element: string, what: string) =>
  notice(`h${n}`, t, { notice: "hand_edit", version: n, elementId, element, what });

const version = (n: number, t: number, extra: Partial<ArtifactVersion> = {}): ArtifactVersion => ({
  id: `v${n}`,
  version: n,
  name: "mock_markets.deck.html",
  createdAt: at(t),
  origin: "ai",
  chatId: CONVERSATION,
  itemId: null,
  ...extra,
});

describe("studioThread", () => {
  const versions = [
    version(6, 50),
    version(5, 42, { origin: "manual", editSummary: "slide 3 (region-chart): duplicated" }),
    version(4, 41, { origin: "manual", editSummary: "slide 3 title color #111→#c33" }),
    version(3, 40, { origin: "manual", editSummary: "slide 3 title font-size 26px→32px" }),
    version(2, 20, { chatId: STUDIO, itemId: "m1", fromStudio: true }),
    version(1, 0),
  ];
  const messages = [
    message("m1", "user", "Make this a bar chart, biggest first.", 10),
    message("a1", "bot", "Done: slide 3 is now a bar chart.", 30),
    // The engine records each hand edit a moment after its version.
    handEdit(3, 40.2, "title", "the title", "You made the title larger"),
    handEdit(4, 41.2, "title", "the title", "You changed the colour of the title"),
    handEdit(5, 42.2, "region-chart", "slide 3", "You duplicated slide 3"),
  ];

  it("merges the chat and the versions by time, a chat save after its reply", () => {
    const rows = studioThread(messages, versions, STUDIO);
    expect(rows.map((row) => (row.kind === "version" ? `v${row.version}` : row.kind))).toEqual([
      "v1",
      "me",
      "ai",
      "v2",
      "hand",
      "v4",
      "hand",
      "v5",
      "v6",
    ]);
  });

  it("names each version's origin in the five studio words", () => {
    const origins = studioThread(messages, versions, STUDIO).flatMap((row) =>
      row.kind === "version" ? [`v${row.version}:${row.origin}`] : [],
    );
    expect(origins).toEqual(["v1:superchat", "v2:chat", "v4:you", "v5:you", "v6:superchat"]);
    expect(versionOrigin(version(7, 60, { origin: "restore" }), STUDIO)).toBe("restored");
    expect(versionOrigin(version(8, 61, { origin: "update" }), STUDIO)).toBe("updated");
  });

  it("folds consecutive hand edits to one element by its id, and keeps the plain words", () => {
    const hands = studioThread(messages, versions, STUDIO).filter((row) => row.kind === "hand");
    expect(hands).toMatchObject([
      { elementId: "title", element: "the title", count: 2, from: 3, to: 4 },
      { elementId: "region-chart", count: 1, from: 5, to: 5, what: "You duplicated slide 3" },
    ]);
    // Never the CSS of the edit, which only the version's summary carries.
    expect(JSON.stringify(hands)).not.toContain("#c33");
  });

  it("does not fold edits to different elements, or a hand edit with no element", () => {
    const rows = studioThread(
      [
        handEdit(3, 40.2, "title", "the title", "You made the title larger"),
        notice("h4", 41.2, { notice: "hand_edit", version: 4, what: "You switched the theme" }),
        notice("h5", 42.2, { notice: "hand_edit", version: 5, what: "You restored the file" }),
      ],
      versions.slice(1, 4),
      STUDIO,
    );
    expect(
      rows.filter((row) => row.kind === "hand").map((row) => row.kind === "hand" && row.count),
    ).toEqual([1, 1, 1]);
  });

  it("puts a since-you-were-here row above the first version newer than the last visit", () => {
    const rows = studioThread(messages, versions, STUDIO, 3);
    const kinds = rows.map((row) => (row.kind === "version" ? `v${row.version}` : row.kind));
    // The folded v3 → v4 run reaches past v3, so the row goes above it.
    expect(kinds.slice(3)).toEqual(["v2", "since", "hand", "v4", "hand", "v5", "v6"]);
    expect(studioThread(messages, versions, STUDIO, 6).some((row) => row.kind === "since")).toBe(
      false,
    );
    expect(studioThread(messages, versions, STUDIO).some((row) => row.kind === "since")).toBe(
      false,
    );
  });

  it("shows a request's element chip instead of its request block, and the slide it is about", () => {
    const { block } = buildElementAsk({
      artifactId: "v2",
      name: "mock_markets.deck.html",
      version: 2,
      elements: [{ id: "region-chart", label: "Chart", slide: 3, text: null, style: {} }],
      noun: (n) => `${n} elements`,
    });
    const asked = message("m2", "user", `${block}\n\nShow the share next to each bar.`, 60);
    const rows = studioThread([asked], [], STUDIO);
    expect(rows[0]).toMatchObject({
      kind: "me",
      chip: "Slide 3 · region-chart",
      text: "Show the share next to each bar.",
      slide: 3,
    });
  });
});

describe("notices", () => {
  it("shows a version saved elsewhere as a quiet note in its place, and drops version records", () => {
    const rows = studioThread(
      [
        message("m1", "user", "Biggest first", 10),
        notice("n1", 12, { notice: "stale", version: 9, origin: "superchat" }),
        // The version list draws these.
        notice("r2", 13, { notice: "version", version: 9 }),
        message("a1", "bot", "Sorted the bars.", 14),
      ],
      [],
      STUDIO,
    );
    expect(rows.map((row) => row.kind)).toEqual(["me", "note", "ai"]);
    expect(rows[1]).toMatchObject({ kind: "note", notice: "stale", version: 9 });
  });
});
