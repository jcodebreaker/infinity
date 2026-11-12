import { describe, expect, it } from "vitest";
import { copyableMessageText } from "../../../lib/message-text";
import { FOLD_AT, foldedHead } from "./FoldedText";

describe("foldedHead", () => {
  it("keeps a text at or under the fold whole", () => {
    const text = "a".repeat(FOLD_AT);
    expect(foldedHead(text)).toBe(text);
  });

  it("cuts a long text near the fold at a word break", () => {
    const head = foldedHead("word ".repeat(1_000));
    expect(head.length).toBeLessThanOrEqual(FOLD_AT);
    expect(head.length).toBeGreaterThan(FOLD_AT * 0.8);
    expect(head.endsWith("word")).toBe(true);
  });

  it("never splits an emoji in two", () => {
    const head = foldedHead(`${"a".repeat(FOLD_AT - 1)}😀${"b".repeat(10)}`);
    expect(head).toBe("a".repeat(FOLD_AT - 1));
  });
});

it("copies the whole message, not the folded view", () => {
  const long = "x".repeat(440_000);
  const text = copyableMessageText({
    id: "m1",
    threadId: "s1",
    seq: 1,
    role: "user",
    blocks: [{ kind: "text", text: long }],
    createdAt: "2026-10-01T10:00:00.000Z",
  } as never);
  expect(text).toBe(long);
});
