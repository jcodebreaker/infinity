import { describe, expect, it, vi } from "vitest";
import { createChatOwnershipResolver, resolveChatOwnership } from "./chat-ownership.js";
import { OmnigentApiError } from "./client/core.js";

const { getOmnigentSession } = vi.hoisted(() => ({ getOmnigentSession: vi.fn() }));

vi.mock("./client/sessions.js", () => ({ getOmnigentSession }));

const CLIENT = { baseUrl: "http://omnigent.test", proxySecret: "secret", tenant: "space-1" };
const EMAIL = "person@example.test";

describe("resolveChatOwnership", () => {
  const superChats = [{ botId: "bot-1", omnigentSessionId: "conv_super" }];
  const session = (
    id: string,
    superchat: Record<string, unknown> | null,
    status = "idle",
  ): Record<string, unknown> => ({ id, status, superchat });
  const family = (kind: string, root: string) => ({
    kind,
    root_id: root,
    parent_id: root,
    project: null,
  });

  it("resolves a Helper at any depth by its family root", async () => {
    getOmnigentSession.mockResolvedValue(session("conv_helper", family("helper", "conv_super")));
    const ownership = await resolveChatOwnership(CLIENT, EMAIL, superChats, "conv_helper");
    expect(ownership).toEqual({ botId: "bot-1" });
  });

  it("returns null outside the caller's own Conversations, outside any family, or for any chat that is not a Helper", async () => {
    getOmnigentSession.mockResolvedValue(session("x", family("helper", "conv_other")));
    expect(await resolveChatOwnership(CLIENT, EMAIL, superChats, "x")).toBeNull();
    getOmnigentSession.mockResolvedValue(session("x", null));
    expect(await resolveChatOwnership(CLIENT, EMAIL, superChats, "x")).toBeNull();
    getOmnigentSession.mockResolvedValue(session("x", family("super", "conv_super")));
    expect(await resolveChatOwnership(CLIENT, EMAIL, superChats, "x")).toBeNull();
  });

  it("a resolver reads each chat's session once", async () => {
    getOmnigentSession.mockClear();
    getOmnigentSession.mockImplementation(async (_c: unknown, _e: string, id: string) =>
      session(id, family("helper", "conv_super")),
    );
    const resolve = createChatOwnershipResolver(CLIENT, EMAIL, superChats);
    const found = await Promise.all(["conv_h1", "conv_h2", "conv_h1", "conv_h2"].map(resolve));
    expect(found.every((o) => o?.botId === "bot-1")).toBe(true);
    expect(getOmnigentSession).toHaveBeenCalledTimes(2);
  });

  it("treats a session the engine no longer has as unowned but surfaces other failures", async () => {
    getOmnigentSession.mockRejectedValueOnce(new OmnigentApiError("gone (404)", "not_found"));
    expect(await resolveChatOwnership(CLIENT, EMAIL, superChats, "conv_gone")).toBeNull();
    getOmnigentSession.mockRejectedValueOnce(new OmnigentApiError("boom (500)", undefined));
    await expect(resolveChatOwnership(CLIENT, EMAIL, superChats, "conv_x")).rejects.toThrow("boom");
  });

  it("never resolves the Conversation's own id as a chat", async () => {
    getOmnigentSession.mockResolvedValue(
      session("conv_super", { ...family("super", "conv_super"), parent_id: null }),
    );
    expect(await resolveChatOwnership(CLIENT, EMAIL, superChats, "conv_super")).toBeNull();
  });
});
