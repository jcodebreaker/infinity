import { describe, expect, it } from "vitest";
import { versionMessageId } from "./artifacts.js";
import type { OmnigentTranscriptPage } from "./client/transcript.js";
import { mapReplyFrame, mapTranscriptMessage, mapTranscriptPage } from "./transcript.js";

const page = (data: OmnigentTranscriptPage["data"], hasMore = false): OmnigentTranscriptPage => ({
  data,
  has_more: hasMore,
  older_cursor: hasMore ? "cursor-1" : null,
  lineage: { kind: "super", root_id: "s1", parent_id: null },
});

describe("mapTranscriptPage", () => {
  it("maps each engine block onto the chat's existing block types", () => {
    const result = mapTranscriptPage(
      "s1",
      page([
        { id: "m1", role: "user", created_at: 100, blocks: [{ type: "text", text: "hi" }] },
        {
          id: "m2",
          role: "assistant",
          created_at: 101,
          blocks: [
            { type: "text", text: "On it." },
            {
              type: "helper",
              call_id: "c1",
              session_id: "h1",
              title: "Research",
              status: "running",
            },
          ],
        },
        {
          id: "m3",
          role: "assistant",
          created_at: 102,
          blocks: [
            {
              type: "card",
              card_id: "q1",
              // The engine's card id names the card even when its payload carries none.
              card: { card: "quote", data: { n: 1 }, fallback: "n 1" },
              pending: true,
            },
          ],
        },
        {
          id: "m4",
          role: "assistant",
          created_at: 103,
          blocks: [
            {
              type: "file",
              artifact_id: "a1",
              name: "plan.md",
              mime: "text/markdown",
              kind: "markdown",
              size: 12,
              version: 2,
            },
          ],
        },
        {
          id: "m5",
          role: "assistant",
          created_at: 104,
          blocks: [{ type: "secure_entry", request_id: "r1", name: "Login", site: "example.test" }],
        },
        {
          id: "m6",
          role: "assistant",
          created_at: 105,
          blocks: [{ type: "error", code: "x", level: "info" }],
        },
      ]),
    );
    expect(result.messages.map((message) => message.role)).toEqual([
      "user",
      "bot",
      "bot",
      "bot",
      "bot",
      "bot",
    ]);
    expect(result.messages[0]?.createdAt).toBe("1970-01-01T00:01:40.000Z");
    expect(result.messages[1]?.blocks).toEqual([
      { kind: "text", text: "On it." },
      { kind: "helper", helperId: "h1", title: "Research", status: "running" },
    ]);
    expect(result.messages[2]?.blocks).toEqual([
      {
        kind: "reply_card",
        card: "quote",
        id: "q1",
        data: { n: 1 },
        fallback: "n 1",
        pending: true,
      },
    ]);
    expect(result.messages[3]?.blocks[0]).toMatchObject({
      kind: "reply_card",
      card: "file",
      id: "artifact:a1",
      data: { name: "plan.md", artifactId: "a1", kind: "markdown", size: 12, version: 2 },
    });
    expect(result.messages[4]?.blocks[0]).toMatchObject({
      kind: "reply_card",
      card: "secure_entry",
      data: { requestId: "r1", name: "Login", site: "example.test" },
    });
    expect(result.messages[5]?.blocks).toEqual([{ kind: "error", code: "x", level: "info" }]);
  });

  it("pages back with the engine's cursor and reports a live chat", () => {
    expect(mapTranscriptPage("s1", { ...page([], true), live: true })).toMatchObject({
      threadId: "s1",
      olderItemCursor: "cursor-1",
      running: true,
    });
    expect(mapTranscriptPage("s1", page([])).olderItemCursor).toBeNull();
  });

  it("passes the engine's reset checkpoint through, and none when there is none", () => {
    const cleared = mapTranscriptPage("s1", {
      ...page([]),
      reset: { item_id: "r1", created_at: 60 },
    });
    expect(cleared.reset).toEqual({ itemId: "r1", createdAt: new Date(60_000).toISOString() });
    expect(mapTranscriptPage("s1", page([])).reset).toBeNull();
  });

  it("marks a file the person delivered, and leaves the Muse's own saves unmarked", () => {
    const file = { type: "file" as const, artifact_id: "a1", name: "q3.pptx", mime: null };
    const result = mapTranscriptPage(
      "s1",
      page([
        { id: "m1", role: "assistant", created_at: 1, blocks: [{ ...file, by: "user" }] },
        { id: "m2", role: "assistant", created_at: 2, blocks: [{ ...file, artifact_id: "a2" }] },
      ]),
    );
    const data = (i: number) => {
      const block = result.messages[i]?.blocks[0];
      return block?.kind === "reply_card" ? block.data : null;
    };
    expect(data(0)).toMatchObject({ artifactId: "a1", byYou: true });
    expect(data(1)).not.toHaveProperty("byYou");
  });

  describe("a file card repeating a saved artifact", () => {
    const saved = {
      id: "m2",
      role: "assistant" as const,
      created_at: 2,
      blocks: [
        {
          type: "file" as const,
          artifact_id: "a1",
          name: "superchat.html",
          mime: "text/html",
          kind: "html",
          size: 5200,
          version: 1,
        },
      ],
    };
    const fileCard = (id: string, artifactId: string, version?: number) => ({
      id,
      role: "assistant" as const,
      created_at: 3,
      blocks: [
        {
          type: "card" as const,
          card_id: null,
          card: {
            card: "file",
            data: {
              name: "superchat.html",
              kind: "html",
              artifactId,
              ...(version ? { version } : {}),
            },
            fallback: "f",
          },
        },
      ],
    });
    const user = { id: "u", role: "user" as const, created_at: 1, blocks: [] };
    const done = {
      id: "m4",
      role: "assistant" as const,
      created_at: 4,
      blocks: [{ type: "text" as const, text: "Done." }],
    };

    it("shows the artifact once, with its size", () => {
      const result = mapTranscriptPage("s1", page([user, saved, fileCard("m3", "a1", 1), done]));
      expect(result.messages.map((m) => m.id)).toEqual(["u", "m2", "m4"]);
      expect(result.messages[1]?.blocks[0]).toMatchObject({ data: { size: 5200 } });
    });

    it("keeps a card for another artifact or another version", () => {
      const result = mapTranscriptPage(
        "s1",
        page([user, saved, fileCard("m3", "a2"), fileCard("m5", "a1", 2)]),
      );
      expect(result.messages.map((m) => m.id)).toEqual(["u", "m2", "m3", "m5"]);
    });

    it("shows the card again in a later turn", () => {
      const result = mapTranscriptPage(
        "s1",
        page([user, saved, { ...user, id: "u2" }, fileCard("m3", "a1", 1)]),
      );
      expect(result.messages.map((m) => m.id)).toEqual(["u", "m2", "u2", "m3"]);
    });
  });
});

describe("a file's studio chat (ADR 0024)", () => {
  it("keeps the engine's runtime notes as system messages, and every other role as before", () => {
    const result = mapTranscriptPage(
      "studio-1",
      page([
        {
          id: "m1",
          role: "user",
          created_at: 100,
          blocks: [{ type: "text", text: "Biggest first" }],
        },
        {
          id: "n1",
          role: "system",
          created_at: 101,
          blocks: [{ type: "notice", kind: "stale", version: 9, origin: "superchat" }],
        },
        {
          id: "n2",
          role: "system",
          created_at: 102,
          blocks: [
            {
              type: "notice",
              kind: "hand_edit",
              version: 10,
              element_id: "cover-title",
              element: "the title",
              what: "You made the title larger",
            },
          ],
        },
        { id: "a1", role: "assistant", created_at: 103, blocks: [{ type: "text", text: "Done." }] },
      ]),
    );
    expect(result.messages.map((message) => message.role)).toEqual([
      "user",
      "system",
      "system",
      "bot",
    ]);
    expect(result.messages[1]?.blocks).toEqual([
      { kind: "notice", notice: "stale", version: 9, origin: "superchat" },
    ]);
    expect(result.messages[2]?.blocks).toEqual([
      {
        kind: "notice",
        notice: "hand_edit",
        version: 10,
        elementId: "cover-title",
        element: "the title",
        what: "You made the title larger",
      },
    ]);
  });
});

describe("the engine's display (ADR 0027)", () => {
  it("carries a reply's Activity id", () => {
    const message = mapTranscriptMessage("conv", {
      id: "m1",
      role: "assistant",
      created_at: 100,
      activity_id: "turn:conv:resp_1",
      blocks: [
        {
          type: "card",
          card_id: "progress-1",
          card: { card: "progress", id: "progress-1", data: {}, fallback: "" },
        },
      ],
    });
    expect(message.activityId).toBe("turn:conv:resp_1");
    expect(message.blocks[0]).toEqual(expect.objectContaining({ id: "progress-1" }));
    const plain = mapTranscriptMessage("conv", {
      id: "m2",
      role: "user",
      created_at: 100,
      blocks: [{ type: "text", text: "hi" }],
    });
    expect(plain).not.toHaveProperty("activityId");
  });
});

describe("the reply as it streams", () => {
  it("reads each text delta, flat or wrapped", () => {
    expect(mapReplyFrame({ type: "response.output_text.delta", delta: "Building " })).toEqual({
      kind: "text",
      delta: "Building ",
    });
    expect(
      mapReplyFrame({ type: "response.output_text.delta", data: { delta: "the deck." } }),
    ).toEqual({ kind: "text", delta: "the deck." });
    expect(mapReplyFrame({ type: "response.output_text.delta", delta: "" })).toBeNull();
  });

  it("reads the in-flight text replayed on (re)connect as one delta of everything so far", () => {
    const snapshot = [
      { type: "session.heartbeat" },
      { type: "response.created", response: { id: "resp_1", status: "in_progress" } },
      { type: "response.output_text.delta", delta: "Building the deck now." },
    ];
    expect(snapshot.map(mapReplyFrame).filter(Boolean)).toEqual([
      { kind: "text", delta: "Building the deck now." },
    ]);
    expect(
      mapReplyFrame({
        type: "response.output_text.delta",
        delta: "So far.",
        message_id: "msg_1",
        index: 4,
      }),
    ).toEqual({ kind: "text", delta: "So far." });
  });

  it("resets when the text is stored as a message or the turn ends", () => {
    expect(
      mapReplyFrame({
        type: "response.output_item.done",
        item: { type: "message", role: "assistant", content: [] },
      }),
    ).toEqual({ kind: "textReset" });
    for (const type of [
      "response.completed",
      "response.failed",
      "response.cancelled",
      "response.incomplete",
    ]) {
      expect(mapReplyFrame({ type })).toEqual({ kind: "textReset" });
    }
  });

  it("ignores every other frame", () => {
    for (const frame of [
      { type: "response.output_item.done", item: { type: "function_call_output", call_id: "c" } },
      { type: "response.output_item.done", item: { type: "function_call", call_id: "c" } },
      { type: "response.reasoning_text.delta", delta: "hmm" },
      { type: "session.status", status: "running" },
      { type: "session.heartbeat" },
      { type: "response.in_progress" },
    ]) {
      expect(mapReplyFrame(frame)).toBeNull();
    }
  });
});

describe("how a message sent mid-turn was taken", () => {
  it("carries the engine's delivered state onto the message, and nothing on the others", () => {
    const result = mapTranscriptPage(
      "s1",
      page([
        { id: "u1", role: "user", created_at: 1, blocks: [], delivered: "queued" },
        { id: "u2", role: "user", created_at: 2, blocks: [] },
      ]),
    );
    expect(result.messages[0]?.delivered).toBe("queued");
    expect(result.messages[1]).not.toHaveProperty("delivered");
  });
});

describe("file search citations", () => {
  it("carries the engine's passages card through as a reply card", () => {
    const items = [
      { artifactId: "a".repeat(32), name: "finance.pdf", page: 2, hasThumbnail: true },
    ];
    const result = mapTranscriptPage(
      "s1",
      page([
        {
          id: "m1",
          role: "assistant",
          created_at: 1,
          blocks: [
            { type: "text", text: "Currency exposure (finance.pdf, p. 2)." },
            {
              type: "card",
              card_id: null,
              card: { card: "passages", data: { items }, fallback: "Sources: finance.pdf p. 2" },
            },
          ],
        },
      ]),
    );
    expect(result.messages[0]?.blocks[1]).toEqual({
      kind: "reply_card",
      card: "passages",
      data: { items },
      fallback: "Sources: finance.pdf p. 2",
    });
  });
});

describe("a message's artifact link", () => {
  it("maps the engine's link, keeps an explicit none, and leaves it out when the engine has none", () => {
    const result = mapTranscriptPage(
      "s1",
      page([
        {
          id: "m1",
          role: "user",
          created_at: 1,
          blocks: [{ type: "text", text: "bigger title" }],
          artifact: { artifact_id: "art_1", name: "Q3 Review.html", version: 4, how: "edited" },
        },
        {
          id: "m2",
          role: "user",
          created_at: 2,
          blocks: [{ type: "text", text: "thanks" }],
          artifact: null,
        },
        { id: "m3", role: "user", created_at: 3, blocks: [{ type: "text", text: "hi" }] },
      ]),
    );
    expect(result.messages[0]?.artifact).toEqual({
      artifactId: "art_1",
      name: "Q3 Review.html",
      version: 4,
      how: "edited",
    });
    expect(result.messages[1]?.artifact).toBeNull();
    expect(result.messages[2] && "artifact" in result.messages[2]).toBe(false);
  });

  it("maps one message on its own (the relink answer)", () => {
    expect(
      mapTranscriptMessage("s1", {
        id: "m1",
        role: "user",
        created_at: 0,
        blocks: [],
        artifact: { artifact_id: "art_2", name: "memo.md", version: null, how: "chosen" },
      }),
    ).toMatchObject({
      id: "m1",
      threadId: "s1",
      role: "user",
      artifact: { artifactId: "art_2", name: "memo.md", version: null, how: "chosen" },
    });
  });
});

describe("versionMessageId", () => {
  const base = { id: "v", version: 1, size: 1, created_at: 0 };
  it("takes the engine's item id, else the Conversation's own turn item, never a hand edit's", () => {
    expect(versionMessageId({ ...base, item_id: "msg_1" }, "s1")).toBe("msg_1");
    expect(versionMessageId({ ...base, item_id: null, turn_item_id: "msg_2" }, "s1")).toBeNull();
    expect(versionMessageId({ ...base, chat_id: "s1", turn_item_id: "msg_2" }, "s1")).toBe("msg_2");
    expect(
      versionMessageId({ ...base, chat_id: "helper", turn_item_id: "msg_2" }, "s1"),
    ).toBeNull();
    expect(
      versionMessageId({ ...base, origin: "manual", chat_id: "s1", turn_item_id: "m" }, "s1"),
    ).toBeNull();
  });
});
