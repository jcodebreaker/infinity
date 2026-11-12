import { describe, expect, it, vi } from "vitest";
import { alertForReply, type ReplyAlertDeps } from "./replyAlerts";

function deps(overrides: Partial<ReplyAlertDeps> = {}): ReplyAlertDeps {
  return {
    botId: "bot-1",
    botName: "Superchat",
    conversationId: "conv-1",
    enabled: true,
    focused: () => false,
    readText: vi.fn(async () => "All done."),
    notify: vi.fn(),
    ...overrides,
  };
}

describe("alertForReply", () => {
  it("notifies, with the reply as the body, for a reply in the Conversation", async () => {
    const d = deps();
    await alertForReply({ type: "messageDone", chatId: "conv-1", itemId: "i1" }, d);
    expect(d.notify).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "conv-1:i1",
        threadId: "conv-1",
        botId: "bot-1",
        payload: { role: "bot", blocks: [{ kind: "text", text: "All done." }] },
      }),
      "Superchat",
      true,
    );
  });

  it("ignores a reply in any other chat of the family", async () => {
    const d = deps();
    await alertForReply({ type: "messageDone", chatId: "helper-1", itemId: "i2" }, d);
    expect(d.notify).not.toHaveBeenCalled();
  });

  it("does nothing while the person is looking at the app", async () => {
    const d = deps({ focused: () => true });
    await alertForReply({ type: "messageDone", chatId: "conv-1", itemId: "i1" }, d);
    expect(d.notify).not.toHaveBeenCalled();
  });

  it("ignores every other family event", async () => {
    const d = deps();
    await alertForReply({ type: "chatsChanged" }, d);
    await alertForReply({ type: "heartbeat" }, d);
    expect(d.notify).not.toHaveBeenCalled();
  });

  it("still notifies when the reply text cannot be read", async () => {
    const d = deps({ readText: vi.fn(async () => Promise.reject(new Error("down"))) });
    await alertForReply({ type: "messageDone", chatId: "conv-1", itemId: "i1" }, d);
    expect(d.notify).toHaveBeenCalledWith(
      expect.objectContaining({ payload: { role: "bot", blocks: [] } }),
      "Superchat",
      true,
    );
  });
});
