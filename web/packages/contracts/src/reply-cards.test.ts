import { describe, expect, it } from "vitest";
import { MessageBlock } from "./events.js";
import { isReplyCardKind, parseReplyCard, ReplyCardData } from "./reply-cards.js";

const valid = {
  sources: { items: [{ title: "A", url: "https://a.test" }] },
  compare: { columns: ["X"], rows: [{ label: "Row", cells: ["1"] }] },
  ask: { question: "Which?", options: [{ id: "a", label: "A" }] },
  follow_ups: { suggestions: ["Compare with last year", "Show it by region"] },
  quote: { symbol: "ACME", price: 12.5, changePct: -1.2 },
  person: { name: "Ada" },
  file: { name: "r.pdf", size: 10 },
  secure_entry: { requestId: "req-1", name: "Bank login", site: "bank.example" },
  passages: { items: [{ artifactId: "a".repeat(32), name: "r.pdf", page: 2, hasThumbnail: true }] },
  progress: { label: "Import", value: 40, status: "running" },
  topic: {
    key: "polaris",
    title: "Polaris rollout",
    left_off: "Deck v3 is drafted.",
    covered: [{ date: "2026-10-21", kind: "Made", text: "Steering deck v3" }],
    open: [{ kind: "Open", text: "Budget sign-off" }],
    starters: ["Draft the note to the steering group"],
    last_touched_at: 1792000000,
  },
} as const;

describe("passages card", () => {
  const parse = (item: object) =>
    ReplyCardData.passages.safeParse({ items: [{ name: "r.pdf", page: 1, ...item }] }).success;

  it("takes a fileId, a legacy artifactId, and refuses neither", () => {
    expect(parse({ fileId: "f".repeat(32) })).toBe(true);
    expect(parse({ artifactId: "a".repeat(32) })).toBe(true);
    expect(parse({})).toBe(false);
  });
});

describe("reply cards", () => {
  it("has a fixture for every kind", () => {
    expect(Object.keys(valid).sort()).toEqual(Object.keys(ReplyCardData).sort());
  });

  it.each(Object.entries(valid))("parses a valid %s card", (card, data) => {
    expect(parseReplyCard({ card, data })?.kind).toBe(card);
  });

  it("rejects unknown kinds and malformed data so the fallback is drawn", () => {
    expect(parseReplyCard({ card: "hologram", data: {} })).toBeNull();
    expect(parseReplyCard({ card: "quote", data: { symbol: "A" } })).toBeNull();
    expect(
      parseReplyCard({ card: "progress", data: { ...valid.progress, value: 101 } }),
    ).toBeNull();
    expect(isReplyCardKind("toString")).toBe(false);
  });

  it("bounds follow-ups to 1-3 non-empty short suggestions", () => {
    const parse = (suggestions: string[]) =>
      parseReplyCard({ card: "follow_ups", data: { suggestions } });
    expect(parse(["a"])).not.toBeNull();
    expect(parse([])).toBeNull();
    expect(parse(["a", "b", "c", "d"])).toBeNull();
    expect(parse([""])).toBeNull();
    expect(parse(["x".repeat(121)])).toBeNull();
  });

  it("bounds a clarification (ask) to the options the engine allows", () => {
    const options = (n: number) =>
      Array.from({ length: n }, (_, i) => ({ id: `opt-${i}`, label: `Option ${i}` }));
    expect(
      parseReplyCard({ card: "ask", data: { question: "Q?", options: options(5) } }),
    ).not.toBeNull();
    expect(parseReplyCard({ card: "ask", data: { question: "Q?", options: [] } })).toBeNull();
  });

  it("persists an unknown kind as a block without failing the message", () => {
    const block = { kind: "reply_card", card: "hologram", data: { x: 1 }, fallback: "text" };
    expect(MessageBlock.safeParse(block).success).toBe(true);
    // An older chat's plan card is no kind any more (ADR 0029): it draws its fallback.
    const plan = { card: "plan", data: { items: [{ text: "Do" }] } };
    expect(isReplyCardKind("plan")).toBe(false);
    expect(parseReplyCard(plan)).toBeNull();
  });
});
