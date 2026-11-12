import { call } from "@orpc/server";
import type { RoundView } from "@superchat/contracts";
import { beforeEach, describe, expect, it, vi } from "vitest";

const adapters = vi.hoisted(() => ({
  getOmnigentRound: vi.fn(),
  getOmnigentTranscript: vi.fn(),
  postOmnigentMessage: vi.fn(),
  resolveChatOwnership: vi.fn(),
  streamOmnigentFamily: vi.fn(),
  streamOmnigentSession: vi.fn(),
  resetOmnigentSession: vi.fn(),
  getOmnigentWorkspaceRoot: vi.fn(),
  moveOmnigentSessionForPerson: vi.fn(),
  setOmnigentMessageArtifact: vi.fn(),
}));

// The mapping and error classes stay real so `instanceof` checks and the mapped shapes are
// exercised; only the calls that reach the engine are faked.
vi.mock("@superchat/adapters/omnigent/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@superchat/adapters/omnigent/browser")>()),
  ...adapters,
}));
vi.mock("./client", () => ({
  engineClient: () => ({ baseUrl: "http://engine.test", proxySecret: "", secrets: [] }),
  museSessionId: async () => "conv_super",
  NO_EMAIL: "",
}));

const { OmnigentApiError } = await import("@superchat/adapters/omnigent/browser");
const { chatsRouter } = await import("./chats");

const OWNED = { botId: "conv_super" };
const run = <T>(procedure: unknown, input: unknown) =>
  call(procedure as never, input as never) as Promise<T>;

beforeEach(() => vi.resetAllMocks());

describe("chats.setFolder", () => {
  const ROOT = "/home/u/workspace";

  it("moves the Conversation for the person, and back to the whole workspace without the mark", async () => {
    adapters.getOmnigentWorkspaceRoot.mockResolvedValue(ROOT);
    await run(chatsRouter.chats.setFolder, { botId: "conv_super", folder: "reports" });
    expect(adapters.moveOmnigentSessionForPerson).toHaveBeenLastCalledWith(
      expect.anything(),
      "",
      "conv_super",
      `${ROOT}/reports`,
      true,
    );
    await run(chatsRouter.chats.setFolder, { botId: "conv_super", folder: null });
    expect(adapters.moveOmnigentSessionForPerson).toHaveBeenLastCalledWith(
      expect.anything(),
      "",
      "conv_super",
      ROOT,
      false,
    );
  });

  it("refuses a folder outside the workspace and a Muse that is not the person's", async () => {
    adapters.getOmnigentWorkspaceRoot.mockResolvedValue(ROOT);
    await expect(
      run(chatsRouter.chats.setFolder, { botId: "conv_super", folder: "../etc" }),
    ).rejects.toThrow(/outside the workspace/);
    await expect(run(chatsRouter.chats.setFolder, { botId: "other", folder: "a" })).rejects.toThrow(
      /not found/i,
    );
    expect(adapters.moveOmnigentSessionForPerson).not.toHaveBeenCalled();
  });
});

describe("chats.transcript", () => {
  const page = { data: [], has_more: false, live: false };

  it("reads the Conversation when no chat is named and forwards the paging", async () => {
    adapters.getOmnigentTranscript.mockResolvedValue(page);
    await run(chatsRouter.chats.transcript, {
      botId: "conv_super",
      before: "cur_1",
      beforeReset: true,
    });
    expect(adapters.getOmnigentTranscript).toHaveBeenCalledWith(
      expect.anything(),
      "",
      "conv_super",
      {
        limit: 50,
        before: "cur_1",
        beforeReset: true,
      },
    );
  });

  it("marks a Helper's transcript read-only and refuses another Muse's chat", async () => {
    adapters.getOmnigentTranscript.mockResolvedValue(page);
    adapters.resolveChatOwnership.mockResolvedValueOnce(OWNED);
    const result = await run<{ readOnly?: boolean }>(chatsRouter.chats.transcript, {
      botId: "conv_super",
      chatId: "conv_h",
    });
    expect(result.readOnly).toBe(true);
    adapters.resolveChatOwnership.mockResolvedValueOnce({ ...OWNED, botId: "other" });
    await expect(
      run(chatsRouter.chats.transcript, { botId: "conv_super", chatId: "conv_z" }),
    ).rejects.toThrow(/Chat not found/);
  });
});

describe("chats.watch", () => {
  async function collect(signal?: AbortSignal) {
    const events: unknown[] = [];
    const stream = await run<AsyncIterable<unknown>>(chatsRouter.chats.watch, {
      botId: "conv_super",
    });
    void signal;
    for await (const event of stream) events.push(event);
    return events;
  }

  it("relays the engine's family events as ids only", async () => {
    adapters.streamOmnigentFamily.mockImplementation(async function* () {
      yield { type: "message.done", chat_id: "c1", item_id: "i1" };
      yield { type: "message.delivery", chat_id: "c1", item_id: "i2" };
      yield { type: "chat.reset", chat_id: "c1", item_id: "i3" };
      yield { type: "turn.done", chat_id: "c1", status: "completed" };
      yield { type: "chats.changed" };
      yield { type: "activities.changed" };
      yield {
        type: "round.updated",
        root_id: "conv_super",
        round_id: "r1",
        status: "running",
        started_at: 1.5,
        live: "name=deck",
        helpers: 1,
        files: 0,
        steps: 2,
      };
      yield { type: "session.heartbeat" };
    });
    expect(await collect()).toEqual([
      { type: "open" },
      { type: "messageDone", chatId: "c1", itemId: "i1" },
      { type: "messageDelivery", chatId: "c1", itemId: "i2" },
      { type: "chatReset", chatId: "c1", itemId: "i3" },
      { type: "turnDone", chatId: "c1", status: "completed" },
      { type: "chatsChanged" },
      { type: "activitiesChanged" },
      {
        type: "roundUpdated",
        roundId: "r1",
        status: "running",
        startedAt: 1500,
        live: "name=deck",
        helpers: 1,
        files: 0,
        steps: 2,
      },
      { type: "heartbeat" },
    ]);
  });
});

describe("chats.round", () => {
  it("reads the Conversation's Round in ms, its Helpers in the order they started", async () => {
    const helper = (id: string, started: number) => ({
      id,
      title: id,
      folder: null,
      status: "running",
      started_at: started,
      finished_at: null,
    });
    adapters.getOmnigentRound.mockResolvedValue({
      round_id: "r1",
      status: "running",
      started_at: 10,
      settled_at: null,
      elapsed: 2.5,
      stop_line: null,
      now: { actor: "helper", helper_id: "h2", name: "artifact_save", digest: "name=deck" },
      trail: [
        {
          t: 11,
          actor: "assistant",
          helper_id: null,
          name: "start_helper",
          digest: null,
          ok: true,
        },
      ],
      helpers: { h2: helper("h2", 12), h1: helper("h1", 11) },
      files: [{ name: "deck", artifact_id: "a1", version: 2, helper_id: "h2" }],
      checklist: [{ id: "1", subject: "Outline", active_form: "Outlining", status: "in_progress" }],
    });
    const view = await run<RoundView>(chatsRouter.chats.round, {
      botId: "conv_super",
      roundId: "r1",
    });
    expect(adapters.getOmnigentRound).toHaveBeenCalledWith(
      expect.anything(),
      "",
      "conv_super",
      "r1",
    );
    expect(view).toMatchObject({
      startedAt: 10_000,
      settledAt: null,
      elapsedMs: 2_500,
      now: { helperId: "h2", name: "artifact_save", digest: "name=deck" },
      trail: [{ at: 11_000, name: "start_helper", ok: true }],
      files: [{ artifactId: "a1", name: "deck", version: 2, helperId: "h2" }],
      checklist: [{ subject: "Outline", activeForm: "Outlining", status: "in_progress" }],
    });
    expect(view.helpers.map((h) => h.id)).toEqual(["h1", "h2"]);
  });
});

describe("chats.watchReply", () => {
  async function collect(input: { botId: string; chatId: string }) {
    const events: unknown[] = [];
    for await (const event of await run<AsyncIterable<unknown>>(
      chatsRouter.chats.watchReply,
      input,
    )) {
      events.push(event);
    }
    return events;
  }

  it("streams the Conversation's reply as text and resets, nothing else", async () => {
    adapters.streamOmnigentSession.mockImplementation(async function* () {
      yield { type: "session.heartbeat" };
      yield { type: "response.output_text.delta", delta: "Building " };
      yield { type: "response.output_text.delta", delta: "the deck." };
      yield { type: "response.output_item.done", item: { type: "function_call" } };
      yield { type: "response.output_item.done", item: { type: "message" } };
      yield { type: "session.status", status: "running" };
      yield { type: "response.completed" };
    });
    expect(await collect({ botId: "conv_super", chatId: "conv_super" })).toEqual([
      { kind: "text", delta: "Building " },
      { kind: "text", delta: "the deck." },
      { kind: "textReset" },
      { kind: "textReset" },
    ]);
    expect(adapters.streamOmnigentSession).toHaveBeenCalledWith(
      expect.anything(),
      "",
      "conv_super",
      undefined,
    );
    expect(adapters.resolveChatOwnership).not.toHaveBeenCalled();
  });

  it("streams one of the Muse's Helpers, and refuses another Muse's chat", async () => {
    adapters.streamOmnigentSession.mockImplementation(async function* () {});
    adapters.resolveChatOwnership.mockResolvedValueOnce(OWNED);
    expect(await collect({ botId: "conv_super", chatId: "conv_h" })).toEqual([]);
    adapters.resolveChatOwnership.mockResolvedValueOnce(null);
    await expect(collect({ botId: "conv_super", chatId: "conv_z" })).rejects.toThrow(
      /Chat not found/,
    );
    await expect(collect({ botId: "other", chatId: "other" })).rejects.toThrow(/not found/i);
    expect(adapters.streamOmnigentSession).toHaveBeenCalledTimes(1);
  });
});

describe("chats.reset", () => {
  it("answers CONFLICT while a turn is running", async () => {
    adapters.resetOmnigentSession.mockRejectedValue(new OmnigentApiError("busy", "conflict"));
    await expect(run(chatsRouter.chats.reset, { botId: "conv_super" })).rejects.toMatchObject({
      code: "CONFLICT",
    });
  });
});

describe("chats.send", () => {
  it("sends to the Conversation", async () => {
    await run(chatsRouter.chats.send, { botId: "conv_super", text: "hello" });
    expect(adapters.postOmnigentMessage).toHaveBeenCalledWith(
      expect.anything(),
      "",
      "conv_super",
      "hello",
    );
  });

  it("writes a reference line per attached file and folder, then the caption", async () => {
    await run(chatsRouter.chats.send, {
      botId: "conv_super",
      text: "look",
      attachments: [
        { path: "your_files/uploads/a.pdf", name: "a.pdf", mimeType: "application/pdf", size: 12 },
      ],
      folders: ["reports"],
    });
    const text = adapters.postOmnigentMessage.mock.calls[0]?.[3] as string;
    expect(text).toContain(
      "Attached file in your workspace: your_files/uploads/a.pdf (application/pdf, 12 bytes)",
    );
    expect(text).toContain("Attached folder in your workspace: reports/");
    expect(text.endsWith("look")).toBe(true);
  });

  it("passes the artifact open beside the chat to the engine", async () => {
    await run(chatsRouter.chats.send, {
      botId: "conv_super",
      text: "bigger",
      context: { openArtifact: { artifactId: "art_1", version: 2 } },
    });
    expect(adapters.postOmnigentMessage).toHaveBeenCalledWith(
      expect.anything(),
      "",
      "conv_super",
      "bigger",
      { openArtifact: { artifactId: "art_1", version: 2 } },
    );
  });

  it("refuses a file outside the uploads folder and a Muse that is not the person's", async () => {
    await expect(
      run(chatsRouter.chats.send, {
        botId: "conv_super",
        attachments: [
          { path: "secrets/a.pdf", name: "a.pdf", mimeType: "application/pdf", size: 1 },
        ],
      }),
    ).rejects.toThrow(/not an uploaded file/);
    await expect(run(chatsRouter.chats.send, { botId: "other", text: "hi" })).rejects.toThrow(
      /Muse not found/,
    );
    expect(adapters.postOmnigentMessage).not.toHaveBeenCalled();
  });
});

describe("chats.setMessageArtifact", () => {
  it("relinks the message and answers with it as the transcript shows it", async () => {
    adapters.setOmnigentMessageArtifact.mockResolvedValue({
      id: "item_1",
      role: "user",
      created_at: 100,
      blocks: [{ type: "text", text: "bigger" }],
      artifact: { artifact_id: "art_2", name: "memo.md", version: null, how: "chosen" },
    });
    const message = await run<{ id: string; artifact: unknown }>(
      chatsRouter.chats.setMessageArtifact,
      { botId: "conv_super", itemId: "item_1", artifactId: "art_2" },
    );
    expect(adapters.setOmnigentMessageArtifact).toHaveBeenCalledWith(
      expect.anything(),
      "",
      "conv_super",
      "item_1",
      "art_2",
    );
    expect(message).toMatchObject({
      id: "item_1",
      artifact: { artifactId: "art_2", name: "memo.md", version: null, how: "chosen" },
    });
  });

  it("is not found for a message the engine does not have, or another Muse", async () => {
    adapters.setOmnigentMessageArtifact.mockRejectedValue(
      new OmnigentApiError("gone (404)", "not_found"),
    );
    await expect(
      run(chatsRouter.chats.setMessageArtifact, {
        botId: "conv_super",
        itemId: "x",
        artifactId: null,
      }),
    ).rejects.toMatchObject({ code: "NOT_FOUND" });
    await expect(
      run(chatsRouter.chats.setMessageArtifact, { botId: "other", itemId: "x", artifactId: null }),
    ).rejects.toThrow(/Muse not found/);
  });
});
