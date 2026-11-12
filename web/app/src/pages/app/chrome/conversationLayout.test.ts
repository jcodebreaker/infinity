import { describe, expect, it } from "vitest";
import { conversationLayout } from "./conversationLayout";

const empty = { hasMuse: true, loaded: true, messageCount: 0, running: false };

describe("conversationLayout", () => {
  it("is the start page for the Muse's loaded, empty, idle Conversation", () => {
    expect(conversationLayout(empty)).toBe("start");
  });

  it("settles into the thread once there is a message", () => {
    expect(conversationLayout({ ...empty, messageCount: 1 })).toBe("thread");
  });

  it("stays on the thread while a first reply is running, before any message is listed", () => {
    expect(conversationLayout({ ...empty, running: true })).toBe("thread");
  });

  it("never flashes the start page before the transcript has loaded", () => {
    expect(conversationLayout({ ...empty, loaded: false })).toBe("thread");
  });

  it("is the thread outside the Muse's own Conversation", () => {
    expect(conversationLayout({ ...empty, hasMuse: false })).toBe("thread");
  });
});
