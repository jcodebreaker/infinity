import type { MessageBlock, ThreadMessage } from "@superchat/contracts";
import { afterEach, expect, it, vi } from "vitest";
import { activity, helper } from "./activityTestKit";
import { noteServerClock, resetServerClock, serverNow } from "./superchatWork";
import type { TurnOutcome } from "./workPlan";
import {
  answeredWhileWorking,
  composeStrip,
  deriveWorkStrip,
  formatClock,
  helperActivities,
  UNLISTED_GRACE_MS,
  unlistedHelperIds,
} from "./workStripState";

afterEach(() => {
  vi.useRealTimers();
  resetServerClock();
});

const at = (minute: number) => `2026-10-03T11:${String(minute).padStart(2, "0")}:00.000Z`;

function message(
  id: string,
  role: "user" | "bot",
  minute: number,
  blocks: MessageBlock[] = [{ kind: "text", text: id }],
): ThreadMessage {
  return { id, threadId: "conv", seq: 0, role, blocks, createdAt: at(minute) };
}

const startedWork = (minute: number) =>
  message("reply-1", "bot", minute, [
    { kind: "text", text: "I'll write the deck." },
    { kind: "helper", helperId: "h1", title: "Write the deck", status: "in_progress" },
  ]);

it("reads a span as a clock", () => {
  expect(formatClock(0)).toBe("0:00");
  expect(formatClock(41_900)).toBe("0:41");
  expect(formatClock(134_000)).toBe("2:14");
  expect(formatClock(3_729_000)).toBe("1:02:09");
});

it("names the running Helper to focus, else the first listed one", () => {
  const helpers = [
    { helperId: "h1", title: "One" },
    { helperId: "h2", title: "Two" },
  ];
  const working = deriveWorkStrip(
    helpers,
    [helper("h1", { status: "done" }), helper("h2", { status: "in_progress", finishedAt: null })],
    Date.parse(at(30)),
  );
  expect(working.focusId).toBe("sub_agent:h2");
  const settled = deriveWorkStrip(helpers, [helper("h1", { status: "failed" })], 0);
  expect(settled).toMatchObject({ kind: "settled", outcome: "failed", focusId: "sub_agent:h1" });
});

it("marks the reply to a question asked while a Helper ran", () => {
  const messages = [
    message("ask-1", "user", 0),
    startedWork(1),
    message("ask-2", "user", 3),
    message("reply-2", "bot", 4),
  ];
  const running = [helper("h1", { status: "in_progress", startedAt: at(1), finishedAt: null })];
  expect([...answeredWhileWorking(messages, running)]).toEqual(["reply-2"]);
  // The Helper still counts once it is done: the reply came while it ran.
  const done = [helper("h1", { status: "done", startedAt: at(1), finishedAt: at(10) })];
  expect([...answeredWhileWorking(messages, done)]).toEqual(["reply-2"]);
});

it("leaves the hand-back, later replies and replies after the work alone", () => {
  const helpers = [helper("h1", { status: "done", startedAt: at(1), finishedAt: at(5) })];
  // The hand-back follows a runtime wake, not a person's message.
  expect(
    answeredWhileWorking(
      [message("ask-1", "user", 0), startedWork(1), message("hand-back", "bot", 6)],
      helpers,
    ).size,
  ).toBe(0);
  // Asked after the work ended.
  expect(
    answeredWhileWorking(
      [startedWork(1), message("ask-2", "user", 7), message("reply-2", "bot", 8)],
      helpers,
    ).size,
  ).toBe(0);
  // Only the first text reply of the turn, and never a reply that starts work of its own.
  const running = [helper("h1", { status: "in_progress", startedAt: at(1), finishedAt: null })];
  expect([
    ...answeredWhileWorking(
      [
        startedWork(1),
        message("ask-2", "user", 2),
        message("reply-2", "bot", 3),
        message("more", "bot", 4),
        message("ask-3", "user", 5),
        message("new-work", "bot", 6, [
          { kind: "text", text: "Starting that too." },
          { kind: "helper", helperId: "h2", title: "Other", status: "in_progress" },
        ]),
      ],
      running,
    ),
  ]).toEqual(["reply-2"]);
});

it("uses the transcript's status while the feed has not listed the Helper", () => {
  const messages = [startedWork(1), message("ask-2", "user", 2), message("reply-2", "bot", 3)];
  expect([...answeredWhileWorking(messages, undefined)]).toEqual(["reply-2"]);
});

it("marks the answer to a question asked while the work ran, however late the answer", () => {
  const messages = [
    message("ask-1", "user", 0),
    startedWork(1),
    message("ask-2", "user", 3),
    message("reply-2", "bot", 8),
  ];
  const done = [helper("h1", { status: "done", startedAt: at(1), finishedAt: at(5) })];
  expect([...answeredWhileWorking(messages, done)]).toEqual(["reply-2"]);
});

it("counts a card as the answer, so the narration after it is not marked", () => {
  const running = [helper("h1", { status: "in_progress", startedAt: at(1), finishedAt: null })];
  const messages = [
    startedWork(1),
    message("ask-2", "user", 3),
    message("card", "bot", 4, [
      { kind: "reply_card", card: "quote", data: { text: "Ten." }, fallback: "Ten." },
    ]),
    message("narration", "bot", 6),
  ];
  expect(answeredWhileWorking(messages, running).size).toBe(0);
});

it("keeps the window open while a part the Helper handed on still runs", () => {
  const feed = [
    helper("h1", { status: "done", startedAt: at(1), finishedAt: at(2) }),
    helper("h1-part", {
      parentChatId: "h1",
      status: "in_progress",
      startedAt: at(2),
      finishedAt: null,
    }),
  ];
  const messages = [startedWork(1), message("ask-2", "user", 3), message("reply-2", "bot", 4)];
  expect([...answeredWhileWorking(messages, feed)]).toEqual(["reply-2"]);
  expect(deriveWorkStrip([{ helperId: "h1", title: "One" }], feed, 0).kind).toBe("working");
  // Once the part is done, its finish ends the window.
  const finished = [
    feed[0],
    helper("h1-part", { parentChatId: "h1", status: "done", startedAt: at(2), finishedAt: at(4) }),
  ].filter((activity) => activity !== undefined);
  const later = [startedWork(1), message("ask-2", "user", 5), message("reply-2", "bot", 6)];
  expect(answeredWhileWorking(later, finished).size).toBe(0);
});

it("settles a Helper the loaded feed does not list once the turn ends and a grace has passed", () => {
  const foundAt = Date.parse(at(10));
  const old = { helperId: "h-old", title: "Old", status: "in_progress" };
  const helpers = [old];
  const messages = [
    message("reply-1", "bot", 1, [
      { kind: "text", text: "On it." },
      { kind: "helper", ...old },
    ]),
    message("ask-2", "user", 12),
    message("reply-2", "bot", 13),
  ];
  expect(unlistedHelperIds(helpers, [])).toEqual(["h-old"]);
  expect(unlistedHelperIds(helpers, undefined)).toEqual([]);
  // Found missing just now (or not yet noted): it may have started a moment ago.
  expect(deriveWorkStrip(helpers, [], foundAt).kind).toBe("working");
  const firstMissing = new Map([["h-old", foundAt]]);
  const later = foundAt + UNLISTED_GRACE_MS;
  expect(deriveWorkStrip(helpers, [], later, { firstMissing }).kind).toBe("settled");
  expect(answeredWhileWorking(messages, [], false, later, firstMissing).size).toBe(0);
  // While the turn runs it still counts.
  expect(deriveWorkStrip(helpers, [], later, { turnRunning: true, firstMissing }).kind).toBe(
    "working",
  );
  expect([...answeredWhileWorking(messages, [], true, later, firstMissing)]).toEqual(["reply-2"]);
  // Nothing in the Conversation runs: neither does it.
  expect(deriveWorkStrip(helpers, [], foundAt, { lit: false }).kind).toBe("settled");
});

const settledTurn = (outcome: TurnOutcome | undefined) =>
  composeStrip(
    deriveWorkStrip([], [], 0),
    [],
    { live: false, startedAt: 0, endedAt: 1000, ...(outcome ? { outcome } : {}) },
    0,
  );

it("reads how a reply settled, and a waiting answer as a normal finish", () => {
  expect(settledTurn("done").outcome).toBe("done");
  expect(settledTurn("cancelled").outcome).toBe("cancelled");
  expect(settledTurn("failed").outcome).toBe("failed");
  expect(settledTurn("asked").outcome).toBe("done");
  expect(settledTurn(undefined).clockMs).toBe(1000);
  // A failed Helper fails the strip.
  const failed = composeStrip(
    deriveWorkStrip([{ helperId: "h1", title: "One" }], [helper("h1", { status: "failed" })], 0),
    [],
    { live: false, startedAt: 0, endedAt: 1000, outcome: "done" },
    0,
  );
  expect(failed.outcome).toBe("failed");
});

it("counts the live clock on the engine's clock, so settling does not jump", () => {
  const ask = message("ask", "user", 0);
  noteServerClock([ask]);
  // The client runs 7 s ahead: a message the engine wrote at 0:01 arrives at 0:08 on the client.
  const reply = message("reply", "bot", 1);
  noteServerClock([ask, reply], Date.parse(at(1)) + 7_000);
  const start = Date.parse(ask.createdAt);
  const clientNow = Date.parse(at(2)) + 7_000;
  expect(serverNow(clientNow)).toBe(Date.parse(at(2)));
  const turn = { startedAt: start, endedAt: Date.parse(at(2)) };
  const live = composeStrip(deriveWorkStrip([], [], 0), [], { ...turn, live: true }, clientNow);
  const settled = composeStrip(deriveWorkStrip([], [], 0), [], { ...turn, live: false }, clientNow);
  expect(live.clockMs).toBe(120_000);
  expect(settled.clockMs).toBe(120_000);
});

it("never takes a file's studio turn for a Helper: no lit strip, no listing, no answer window", () => {
  const studioTurn = activity({
    id: "turn:studio-1:r1",
    source: "studio",
    chatId: "studio-1",
    artifact: { groupId: "g-deck", name: "q3.deck.html" },
    status: "in_progress",
    startedAt: at(1),
    finishedAt: null,
  });
  const studio = { helperId: "studio-1", title: "Studio", status: "done" };
  expect(deriveWorkStrip([studio], [studioTurn], Date.parse(at(5))).kind).toBe("settled");
  expect(helperActivities([studio], [studioTurn])).toEqual([]);
  const messages = [
    message("ask-1", "user", 0),
    message("reply-1", "bot", 1, [{ kind: "helper", ...studio }]),
    message("ask-2", "user", 3),
    message("reply-2", "bot", 4),
  ];
  expect(answeredWhileWorking(messages, [studioTurn]).size).toBe(0);
});
