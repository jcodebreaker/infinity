import type { ThreadMessage, ThreadMessagePage } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import {
  mergeNewestTranscriptPage,
  optimisticMessage,
  quotedMessageText,
  userCount,
} from "./museTranscript";

const message = (
  id: string,
  createdAt: string,
  role: ThreadMessage["role"] = "bot",
): ThreadMessage => ({
  id,
  threadId: "t",
  seq: 0,
  role,
  blocks: [{ kind: "text", text: id }],
  createdAt,
});
const page = (messages: ThreadMessage[], olderItemCursor: string | null): ThreadMessagePage => ({
  threadId: "t",
  messages,
  olderCursor: null,
  olderItemCursor,
});

describe("mergeNewestTranscriptPage", () => {
  const a = message("a", "2026-10-01T10:00:00Z");
  const b = message("b", "2026-10-01T10:01:00Z");
  const c = message("c", "2026-10-01T10:02:00Z");

  it("starts from the page when nothing is loaded", () => {
    expect(mergeNewestTranscriptPage(null, page([b, c], "b"))).toEqual({
      threadId: "t",
      messages: [b, c],
      olderCursor: "b",
    });
  });

  it("keeps older pages that were loaded and their cursor", () => {
    const merged = mergeNewestTranscriptPage(
      { messages: [a, b], olderCursor: null },
      page([b, c], "b"),
    );
    expect(merged.messages.map((m) => m.id)).toEqual(["a", "b", "c"]);
    expect(merged.olderCursor).toBeNull();
  });

  it("starts over when the new page does not reach back to what was loaded", () => {
    const merged = mergeNewestTranscriptPage(
      { messages: [a], olderCursor: null },
      page([b, c], "b"),
    );
    expect(merged.messages.map((m) => m.id)).toEqual(["b", "c"]);
    expect(merged.olderCursor).toBe("b");
  });
});

describe("quotedMessageText", () => {
  it("puts the quote above the reply as a blockquote, line by line", () => {
    expect(quotedMessageText("one\ntwo", "Yes")).toBe("> one\n> two\n\nYes");
  });

  it("sends just the quote when nothing else was typed", () => {
    expect(quotedMessageText("one", "")).toBe("> one");
  });
});

describe("userCount and optimisticMessage", () => {
  it("counts only the person's messages; none while unread", () => {
    expect(userCount(null)).toBe(0);
    expect(
      userCount([
        message("a", "2026-10-01T10:00:00Z", "user"),
        message("b", "2026-10-01T10:01:00Z"),
      ]),
    ).toBe(1);
  });

  it("shows the person's words as their own message", () => {
    const sent = optimisticMessage("conv", "hello");
    expect(sent.role).toBe("user");
    expect(sent.blocks).toEqual([{ kind: "text", text: "hello" }]);
    expect("artifact" in sent).toBe(false);
  });
});
