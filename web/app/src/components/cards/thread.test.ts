import type { ReplyCardBlock, ThreadMessage } from "@superchat/contracts";
import { expect, it } from "vitest";
import { hiddenFollowUpMessageIds, replyCardKey, resolveReplyCards } from "./thread";

const card = (id: string | undefined, over: Partial<ReplyCardBlock> = {}): ReplyCardBlock => ({
  kind: "reply_card",
  card: "progress",
  id,
  data: { label: "Import", value: 10, status: "running" },
  fallback: "Import 10%",
  ...over,
});
let seq = 0;
const message = (
  role: "bot" | "user",
  ...blocks: ThreadMessage["blocks"]
): ThreadMessage & { id: string } => {
  seq += 1;
  return {
    id: `m${seq}`,
    threadId: "t",
    seq,
    role,
    blocks,
    createdAt: new Date(seq * 1000).toISOString(),
  };
};

it("shows the latest data of a reused id at the first card's position and hides the rest", () => {
  const first = message("bot", card("p1"));
  const reply = message("user", { kind: "text", text: "ok" });
  const update = message(
    "bot",
    card("p1", { data: { label: "Import", value: 80, status: "running" } }),
  );
  const resolved = resolveReplyCards([first, reply, update]);
  expect(resolved.get(replyCardKey(first.id, 0))?.block?.data).toMatchObject({ value: 80 });
  expect(resolved.get(replyCardKey(update.id, 0))?.block).toBeNull();
});

it("keeps the finished card while an update is still pending", () => {
  const first = message("bot", card("p1"));
  const pending = message("bot", card("p1", { pending: true, data: {} }));
  const resolved = resolveReplyCards([first, pending]);
  expect(resolved.get(replyCardKey(first.id, 0))?.block?.pending).toBeUndefined();
});

it("leaves cards without an id alone", () => {
  const a = message("bot", card(undefined), card(undefined));
  const resolved = resolveReplyCards([a]);
  expect([...resolved.values()].every((entry) => entry.block !== null)).toBe(true);
});

it("takes the person's next message as the answer", () => {
  const ask = message("bot", card("a1", { card: "ask" }));
  const unanswered = resolveReplyCards([ask]);
  expect(unanswered.get(replyCardKey(ask.id, 0))?.answer).toBeUndefined();
  const reply = message("user", { kind: "text", text: " Berlin " });
  expect(resolveReplyCards([ask, reply]).get(replyCardKey(ask.id, 0))?.answer).toBe("Berlin");
});

it("keeps follow-up chips on the final message only", () => {
  const chips = (): ReplyCardBlock =>
    card(undefined, { card: "follow_ups", data: { suggestions: ["Next"] } });
  const first = message("bot", chips());
  const second = message("bot", chips());
  expect(resolveReplyCards([first, second]).get(replyCardKey(first.id, 0))?.block).toBeNull();
  expect(resolveReplyCards([first, second]).get(replyCardKey(second.id, 0))?.block).not.toBeNull();
  // A reply from the person after the chips retires them.
  const reply = message("user", { kind: "text", text: "Next" });
  expect(resolveReplyCards([second, reply]).get(replyCardKey(second.id, 0))?.block).toBeNull();
});

it("locks a clarification card on the person's next message", () => {
  const ask = message(
    "bot",
    card("c1", {
      card: "ask",
      data: {
        question: "Q?",
        options: [
          { id: "opt-1", label: "A" },
          { id: "opt-2", label: "B" },
        ],
      },
    }),
  );
  const pick = message("user", { kind: "text", text: "B" });
  expect(resolveReplyCards([ask, pick]).get(replyCardKey(ask.id, 0))?.answer).toBe("B");
  expect(resolveReplyCards([ask]).get(replyCardKey(ask.id, 0))?.answer).toBeUndefined();
});

it("hides rows that hold only follow-up chips that are not shown (no ghost row)", () => {
  const chips = (): ReplyCardBlock =>
    card(undefined, { card: "follow_ups", data: { suggestions: ["Next"] } });
  const answer = message("bot", { kind: "text", text: "Answer" });
  const old = message("bot", chips());
  const reply = message("user", { kind: "text", text: "ok" });
  const latest = message("bot", chips());
  expect([...hiddenFollowUpMessageIds([answer, old, reply, latest], false)]).toEqual([old.id]);
  // While the Muse works the latest chips go too; an answer row is never hidden.
  expect([...hiddenFollowUpMessageIds([answer, old, reply, latest], true)]).toEqual([
    old.id,
    latest.id,
  ]);
  // A message with chips and other content keeps its row.
  const mixed = message("bot", { kind: "text", text: "Hi" }, chips());
  expect(hiddenFollowUpMessageIds([mixed, reply], false).size).toBe(0);
});
