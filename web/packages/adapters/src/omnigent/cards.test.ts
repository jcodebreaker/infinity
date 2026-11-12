import type { ReplyCardBlock } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { redactReplyCard } from "./cards.js";

const block: ReplyCardBlock = {
  kind: "reply_card",
  card: "quote",
  id: "q1",
  data: { symbol: "ACME", price: 12.5 },
  fallback: "ACME 12.5",
};

describe("redactReplyCard", () => {
  it("passes a clean card through", () => {
    expect(redactReplyCard(block, ["hunter2"])).toBe(block);
  });

  it("degrades a card holding a secret to redacted text", () => {
    expect(redactReplyCard(block, ["ACME"])).toEqual({ kind: "text", text: "[redacted] 12.5" });
  });
});
