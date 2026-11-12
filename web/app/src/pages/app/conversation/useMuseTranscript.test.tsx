// @vitest-environment jsdom

import type {
  FamilyEvent,
  ReplyEvent,
  ThreadMessage,
  ThreadMessagePage,
} from "@superchat/contracts";
import { act, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

/** One `chats.watchReply` connection the test drives: `push` an event, `end` the stream. */
type ReplyStream = { push: (event: ReplyEvent) => void; end: () => void };
const reply = vi.hoisted(() => ({ streams: [] as ReplyStream[] }));
function replyStream(): AsyncIterable<ReplyEvent> {
  const queue: Array<ReplyEvent | null> = [];
  let wake: (() => void) | undefined;
  const stream: ReplyStream = {
    push: (event) => {
      queue.push(event);
      wake?.();
    },
    end: () => {
      queue.push(null);
      wake?.();
    },
  };
  reply.streams.push(stream);
  return {
    async *[Symbol.asyncIterator]() {
      while (true) {
        if (queue.length === 0) await new Promise<void>((resolve) => (wake = resolve));
        const event = queue.shift();
        if (event === undefined) continue;
        if (event === null) return;
        yield event;
      }
    },
  };
}
const api = vi.hoisted(() => ({
  chats: {
    transcript: vi.fn(),
    markRead: vi.fn(async () => ({ ok: true })),
    watchReply: vi.fn(),
  },
}));
const family = vi.hoisted(() => ({ listener: null as ((event: FamilyEvent) => void) | null }));
vi.mock("../../../lib/rpc", () => ({ rpc: api }));
vi.mock("../../../lib/family-stream", () => ({
  watchFamily: (_botId: string, listener: (event: FamilyEvent) => void) => {
    family.listener = listener;
    return () => undefined;
  },
  useFamilyLive: () => false,
}));

import { transcriptWork } from "../../../features/activity/workPlan";
import { deriveWorkState } from "../../../features/activity/workState";
import { useMuseTranscript } from "./useMuseTranscript";

const message = (id: string, role: ThreadMessage["role"] = "user"): ThreadMessage => ({
  id,
  threadId: "conv",
  seq: 0,
  role,
  blocks: [{ kind: "text", text: id }],
  createdAt: "2026-10-03T10:00:00.000Z",
});
const page = (ids: string[], extra: Partial<ThreadMessagePage> = {}): ThreadMessagePage => ({
  threadId: "conv",
  messages: ids.map((id) => message(id)),
  olderCursor: null,
  ...extra,
});

let latest: ReturnType<typeof useMuseTranscript>;
const scrollRef: RefObject<HTMLElement | null> = { current: null };
/** What the transcript shows, as Shell composes it. */
const shown = () =>
  [...(latest.messages ?? []), latest.pendingMessage, latest.liveMessage].filter((m) => m !== null);
/** The most times any one text was on screen in a single render. */
let mostCopies = 0;
function Harness() {
  latest = useMuseTranscript("bot-1", scrollRef);
  const texts = shown().flatMap((m) =>
    m.blocks.flatMap((b) => (b.kind === "text" ? [b.text] : [])),
  );
  for (const text of texts) {
    mostCopies = Math.max(mostCopies, texts.filter((other) => other === text).length);
  }
  return null;
}
const roots: Root[] = [];
/** Whether the Conversation reads as running: the work state over the hook's turn status. */
const running = () =>
  deriveWorkState({
    turn: latest.turn,
    sending: false,
    stopping: false,
    activities: [],
    turnStartedAt: null,
  }).running;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  api.chats.transcript.mockReset();
  api.chats.markRead.mockClear();
  api.chats.watchReply.mockReset();
  api.chats.watchReply.mockImplementation(async () => replyStream());
  reply.streams = [];
  mostCopies = 0;
  family.listener = null;
  vi.useFakeTimers();
});
afterEach(async () => {
  vi.useRealTimers();
  await act(async () => {
    for (const root of roots.splice(0)) root.unmount();
  });
});

async function mountHook() {
  const root = createRoot(document.createElement("div"));
  roots.push(root);
  await act(async () => {
    root.render(<Harness />);
  });
  await act(async () => {});
}

it("offers the earlier history after a clear, pages it before the reset, and drops it on the next clear", async () => {
  api.chats.transcript.mockResolvedValueOnce(
    page(["new-1"], { reset: { itemId: "r1", createdAt: "2026-10-03T10:00:00.000Z" } }),
  );
  await mountHook();
  expect(latest.canShowEarlier).toBe(true);
  expect(latest.olderCursor).toBeNull();

  api.chats.transcript.mockResolvedValueOnce(page(["old-1"], { olderItemCursor: "old-0" }));
  await act(async () => {
    await latest.showEarlier();
  });
  expect(api.chats.transcript).toHaveBeenLastCalledWith({ botId: "bot-1", beforeReset: true });
  expect(latest.messages?.map((m) => m.id)).toEqual(["old-1", "new-1"]);
  expect(latest.canShowEarlier).toBe(false);
  expect(latest.olderCursor).toBe("old-0");

  api.chats.transcript.mockResolvedValueOnce(page(["old-0"]));
  await act(async () => {
    await latest.loadOlder();
  });
  expect(api.chats.transcript).toHaveBeenLastCalledWith({
    botId: "bot-1",
    before: "old-0",
    beforeReset: true,
  });

  // Another clear: nothing loaded survives; the Conversation is read afresh.
  api.chats.transcript.mockResolvedValueOnce(
    page([], { reset: { itemId: "r2", createdAt: "2026-10-03T11:00:00.000Z" } }),
  );
  await act(async () => {
    family.listener?.({ type: "chatReset", chatId: "conv", itemId: "r2" });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(latest.messages).toEqual([]);
  expect(latest.canShowEarlier).toBe(true);
});

it("reads again when the engine says how a message sent mid-turn was taken", async () => {
  api.chats.transcript.mockResolvedValueOnce(page(["a"]));
  await mountHook();
  expect(api.chats.transcript).toHaveBeenCalledTimes(1);

  api.chats.transcript.mockResolvedValueOnce(page(["a", "b"]));
  await act(async () => {
    family.listener?.({
      type: "messageDelivery",
      chatId: "conv",
      itemId: "b",
    });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(api.chats.transcript).toHaveBeenCalledTimes(2);
  expect(latest.messages?.map((m) => m.id)).toEqual(["a", "b"]);

  // Another chat's delivery is not this Conversation's business.
  await act(async () => {
    family.listener?.({
      type: "messageDelivery",
      chatId: "other",
      itemId: "x",
    });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(api.chats.transcript).toHaveBeenCalledTimes(2);
});

it("is running while the engine says a reply is being written, and idle after", async () => {
  api.chats.transcript.mockResolvedValueOnce(page(["a"], { running: true }));
  await mountHook();
  expect(running()).toBe(true);

  api.chats.transcript.mockResolvedValueOnce({
    ...page(["a"]),
    messages: [message("a"), message("b", "bot")],
  });
  await act(async () => {
    family.listener?.({ type: "turnDone", chatId: "conv", status: "completed" });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(running()).toBe(false);
});

it("shows a sent message at once and keeps the Conversation running until the reply is in", async () => {
  api.chats.transcript.mockResolvedValueOnce(page([]));
  await mountHook();
  expect(running()).toBe(false);

  await act(async () => latest.beginSend("hello"));
  expect(latest.pendingMessage?.blocks).toEqual([{ kind: "text", text: "hello" }]);
  expect(running()).toBe(true);

  // The engine has recorded the message but not started: the pending copy gives way, the row stays.
  api.chats.transcript.mockResolvedValueOnce(page(["m1"]));
  await act(async () => {
    family.listener?.({ type: "messageDelivery", chatId: "conv", itemId: "m1" });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(latest.pendingMessage).toBeNull();
  expect(running()).toBe(true);

  api.chats.transcript.mockResolvedValueOnce({
    ...page(["m1"]),
    messages: [message("m1"), message("m2", "bot")],
  });
  await act(async () => {
    family.listener?.({ type: "messageDone", chatId: "conv", itemId: "m2" });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(running()).toBe(false);
});

it("stops reading as running at once after a Stop, until the engine's turn ends", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"], { running: true }));
  await mountHook();
  await act(async () => latest.beginSend("hello"));
  expect(running()).toBe(true);

  await act(async () => latest.stopWaiting());
  expect(running()).toBe(false);
  expect(latest.turn).toMatchObject({ stopped: true, cancelled: true });
  // A message already posted stays shown until the transcript has it.
  expect(latest.pendingMessage).not.toBeNull();
  // A read while the turn unwinds does not light it again, however long it takes: no timer.
  await act(async () => {
    family.listener?.({ type: "chatsChanged" });
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(running()).toBe(false);

  // The turn's end clears the Stop; how it ended stays until the next send.
  api.chats.transcript.mockResolvedValue(page(["a", "hello"]));
  await act(async () => {
    family.listener?.({ type: "turnDone", chatId: "conv", status: "incomplete" });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(latest.turn).toMatchObject({ running: false, stopped: false, cancelled: true });
  expect(latest.pendingMessage).toBeNull();

  await act(async () => latest.beginSend("again"));
  expect(latest.turn).toMatchObject({ stopped: false, cancelled: false });
});

it("reads a turn the engine says was cancelled as cancelled", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"], { running: true }));
  await mountHook();
  api.chats.transcript.mockResolvedValue(page(["a"]));
  await act(async () => {
    family.listener?.({ type: "turnDone", chatId: "conv", status: "cancelled" });
  });
  // The turn's end is believed at once, before the read that follows.
  expect(latest.turn).toMatchObject({ running: false, cancelled: true });
  expect(running()).toBe(false);
});

it("takes a message back when the send fails", async () => {
  api.chats.transcript.mockResolvedValueOnce(page(["a"]));
  await mountHook();
  await act(async () => latest.beginSend("hello"));
  await act(async () => latest.failSend());
  expect(latest.pendingMessage).toBeNull();
  expect(running()).toBe(false);
});

it("reads on a timer only while a reply is expected, whatever the stream does", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"]));
  await mountHook();
  expect(api.chats.transcript).toHaveBeenCalledTimes(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(20_000);
  });
  expect(api.chats.transcript).toHaveBeenCalledTimes(1);

  await act(async () => latest.beginSend("hello"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_500);
  });
  expect(api.chats.transcript).toHaveBeenCalledTimes(2);
});

it("marks the Conversation read when a reply lands while it is in view", async () => {
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  api.chats.transcript.mockResolvedValue(page(["a"]));
  await mountHook();
  expect(api.chats.markRead).toHaveBeenCalledWith({ botId: "bot-1" });
  api.chats.markRead.mockClear();
  await act(async () => {
    family.listener?.({ type: "messageDone", chatId: "conv", itemId: "b" });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(api.chats.markRead).toHaveBeenCalledTimes(1);

  vi.spyOn(document, "hasFocus").mockReturnValue(false);
  api.chats.markRead.mockClear();
  await act(async () => {
    family.listener?.({ type: "messageDone", chatId: "conv", itemId: "c" });
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(api.chats.markRead).not.toHaveBeenCalled();
});

const botText = (id: string, text: string): ThreadMessage => ({
  ...message(id, "bot"),
  blocks: [{ kind: "text", text }],
});
const liveText = () =>
  latest.liveMessage?.blocks.map((block) => (block.kind === "text" ? block.text : "")).join("");
/** Drives the newest reply stream, then lets the hook render. */
async function stream(...events: ReplyEvent[]) {
  await act(async () => {
    for (const event of events) reply.streams.at(-1)?.push(event);
    await vi.advanceTimersByTimeAsync(0);
  });
}
async function familyEvent(event: FamilyEvent) {
  await act(async () => {
    family.listener?.(event);
    await vi.advanceTimersByTimeAsync(200);
  });
}

it("listens to the reply stream only while a turn runs", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"]));
  await mountHook();
  expect(api.chats.watchReply).not.toHaveBeenCalled();

  await act(async () => latest.beginSend("hello"));
  expect(api.chats.watchReply).toHaveBeenCalledWith(
    { botId: "bot-1", chatId: "conv" },
    expect.objectContaining({ signal: expect.any(AbortSignal) }),
  );
});

it("shows the reply as it streams, and a refetch does not remove it", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"], { running: true }));
  await mountHook();
  await stream({ kind: "text", delta: "Building " }, { kind: "text", delta: "the deck." });
  expect(latest.liveMessage).toMatchObject({
    id: "live:conv",
    threadId: "conv",
    role: "bot",
    blocks: [{ kind: "text", text: "Building the deck." }],
  });

  await familyEvent({ type: "chatsChanged" });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_500);
  });
  expect(api.chats.transcript).toHaveBeenCalledTimes(3);
  expect(liveText()).toBe("Building the deck.");
});

it("swaps the streamed reply for the stored one in one render, with no duplicate", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"], { running: true }));
  await mountHook();
  await stream({ kind: "text", delta: "Building the deck now." });

  // The engine stored the text: it stays until the read that brings the stored copy.
  await stream({ kind: "textReset" });
  expect(liveText()).toBe("Building the deck now.");

  api.chats.transcript.mockResolvedValue({
    ...page(["a"], { running: true }),
    messages: [message("a"), botText("b", "Building the deck now.")],
  });
  await familyEvent({ type: "messageDone", chatId: "conv", itemId: "b" });
  expect(latest.liveMessage).toBeNull();
  expect(shown().map((m) => m.id)).toEqual(["a", "b"]);
  expect(mostCopies).toBe(1);
});

it("drops the streamed text when a read already has it stored, even without its reset", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"], { running: true }));
  await mountHook();
  await stream({ kind: "text", delta: "Done: the deck is ready." });

  api.chats.transcript.mockResolvedValue({
    ...page(["a"], { running: true }),
    messages: [message("a"), botText("b", "Done: the deck is ready.")],
  });
  await familyEvent({ type: "chatsChanged" });
  expect(latest.liveMessage).toBeNull();
  expect(mostCopies).toBe(1);
});

it("keeps a new sentence streaming beside the turn's stored text", async () => {
  api.chats.transcript.mockResolvedValue({
    ...page(["a"], { running: true }),
    messages: [message("a"), botText("b", "Building the deck now.")],
  });
  await mountHook();
  await stream({ kind: "text", delta: "Checking the layout." });
  await familyEvent({ type: "chatsChanged" });
  expect(liveText()).toBe("Checking the layout.");
});

it("clears the streamed reply when the turn ends", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"], { running: true }));
  await mountHook();
  await stream({ kind: "text", delta: "Let me look" });

  api.chats.transcript.mockResolvedValue(page(["a"]));
  await familyEvent({ type: "turnDone", chatId: "conv", status: "cancelled" });
  expect(latest.liveMessage).toBeNull();
  expect(running()).toBe(false);
});

it("keeps a streamed sentence on screen through a long tool call, with its strip lit", async () => {
  const helper: ThreadMessage = {
    ...message("h", "bot"),
    blocks: [{ kind: "helper", helperId: "helper-1", title: "Research", status: "in_progress" }],
  };
  api.chats.transcript.mockResolvedValue({
    ...page(["a"], { running: true }),
    messages: [message("a"), helper],
  });
  await mountHook();
  await stream({ kind: "text", delta: "Researching both banks first." });

  // A minute of tool work: the timer reads the transcript a dozen times.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  expect(api.chats.transcript.mock.calls.length).toBeGreaterThan(10);
  expect(liveText()).toBe("Researching both banks first.");
  expect(running()).toBe(true);
  const strip = transcriptWork(shown()).turns.get("live:conv");
  expect(strip).toMatchObject({ last: true, helpers: [{ helperId: "helper-1" }] });
});

it("reconnects after a drop and shows the text so far once", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"], { running: true }));
  await mountHook();
  await stream({ kind: "text", delta: "Building " });
  await act(async () => {
    reply.streams.at(-1)?.end();
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(api.chats.watchReply).toHaveBeenCalledTimes(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000);
  });
  expect(api.chats.watchReply).toHaveBeenCalledTimes(2);
  // The engine replays everything written so far, then the live tail.
  await stream({ kind: "text", delta: "Building the deck" }, { kind: "text", delta: " now." });
  expect(liveText()).toBe("Building the deck now.");
});

it("stops listening when the turn ends", async () => {
  api.chats.transcript.mockResolvedValue(page(["a"], { running: true }));
  await mountHook();
  const signal = api.chats.watchReply.mock.calls[0]?.[1]?.signal as AbortSignal;
  expect(signal.aborted).toBe(false);
  api.chats.transcript.mockResolvedValue({
    ...page(["a"]),
    messages: [message("a"), botText("b", "Done.")],
  });
  await familyEvent({ type: "turnDone", chatId: "conv", status: "completed" });
  expect(signal.aborted).toBe(true);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(api.chats.watchReply).toHaveBeenCalledTimes(1);
});
