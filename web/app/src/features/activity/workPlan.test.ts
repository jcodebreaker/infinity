import type { MessageBlock, ThreadMessage } from "@superchat/contracts";
import { expect, it } from "vitest";
import { withRounds } from "./rounds";
import { drawnOnlyMessageIds, outcomeOf, transcriptWork } from "./workPlan";

// The engine's order (engine/omnigent/superchat/transcript/blocks.py): each card is a message of
// its own, in call order, and a turn keeps one text, its last, after its cards; the Helpers it
// started ride on that text.
let seq = 0;
function message(id: string, role: "user" | "bot", blocks: MessageBlock[], at = 0): ThreadMessage {
  return {
    id,
    threadId: "conv",
    seq: seq++,
    role,
    blocks,
    createdAt: new Date(Date.UTC(2026, 9, 17, 10, 0, at)).toISOString(),
  };
}
const text = (value: string): MessageBlock => ({ kind: "text", text: value });
const helperBlock = (id: string): MessageBlock => ({
  kind: "helper",
  helperId: id,
  title: `Helper ${id}`,
  status: "in_progress",
});

it("draws the reply's Helpers in one strip, under the reply", () => {
  const messages = [
    message("ask", "user", [text("Make the deck")], 0),
    message("started", "bot", [helperBlock("h1")], 2),
    message("reply", "bot", [text("The deck is ready.")], 31),
  ];
  const work = transcriptWork(messages);
  expect([...work.turns.keys()]).toEqual(["reply"]);
  const turn = work.turns.get("reply");
  expect(turn?.helpers.map((helper) => helper.helperId)).toEqual(["h1"]);
  expect(turn?.last).toBe(true);
  expect(turn?.outcome).toBe("done");
  expect((turn?.endedAt ?? 0) - (turn?.startedAt ?? 0)).toBe(31_000);
  expect(work.runningSince).toBe(turn?.startedAt);
  expect([...work.drawn]).toEqual(["started:0"]);
});

it("sits where the work first showed while the reply is written, and under its text once it lands", () => {
  const ask = message("ask", "user", [text("Make the deck")], 0);
  const started = message("started", "bot", [helperBlock("h1")], 2);
  const streaming = transcriptWork([ask, started]);
  const answered = transcriptWork([ask, started, message("reply", "bot", [text("Done.")], 40)]);
  expect([...streaming.turns.keys()]).toEqual(["started"]);
  expect([...answered.turns.keys()]).toEqual(["reply"]);
});

it("sits where the Helpers are, and gives each reply its own strip", () => {
  const work = transcriptWork([
    message("ask-1", "user", [text("First")]),
    message("reply-1", "bot", [text("Started."), helperBlock("h1")]),
    message("ask-2", "user", [text("Second")]),
    message("reply-2", "bot", [text("A one-step answer.")]),
  ]);
  expect([...work.turns.keys()]).toEqual(["reply-1"]);
  expect(work.turns.get("reply-1")?.helpers.map((helper) => helper.helperId)).toEqual(["h1"]);
  expect(work.turns.get("reply-1")?.last).toBe(false);
});

it("leaves a reply without a Helper alone, and an older chat's plan card is a card (ADR 0029)", () => {
  const work = transcriptWork([
    message("ask", "user", [text("Hi")]),
    message("old-plan", "bot", [
      { kind: "reply_card", card: "plan", id: "p", data: { items: [] }, fallback: "" },
    ]),
    message("reply", "bot", [text("Hello.")]),
  ]);
  expect(work.turns.size).toBe(0);
  expect(work.drawn.size).toBe(0);
});

it("leaves no row for a message holding only the Helpers its reply's strip draws", () => {
  const messages = [
    message("ask", "user", [text("Research it")], 0),
    message("started", "bot", [helperBlock("h1")], 1),
    message("reply", "bot", [text("A Helper is on it.")], 2),
  ];
  const work = transcriptWork(messages);
  expect([...work.turns.keys()]).toEqual(["reply"]);
  expect([...drawnOnlyMessageIds(messages, work)]).toEqual(["started"]);
});

it("leaves a later reply's strip alone when a Helper's wake narration arrives", () => {
  const messages = [
    message("ask-1", "user", [text("Write the report")], 0),
    message("reply-1", "bot", [text("A Helper is on it."), helperBlock("h1")], 1),
    message("ask-2", "user", [text("And plan the offsite")], 10),
    message("started-2", "bot", [helperBlock("h2")], 11),
    message("reply-2", "bot", [text("Here is the offsite plan.")], 12),
    // The runtime's notice that h1 finished is dropped; its narration follows.
    message("wake", "bot", [text("The report is done.")], 60),
  ];
  const work = transcriptWork(messages);
  const second = work.turns.get("reply-2");
  expect(second?.last).toBe(false);
  expect(second?.endedAt).toBe(Date.parse(messages[4]?.createdAt ?? ""));
  expect(work.turns.get("reply-1")?.last).toBe(false);
  expect(work.runningSince).toBe(Date.parse(messages[5]?.createdAt ?? ""));
});

it("reads how the reply ended", () => {
  const outcome = (...replies: ThreadMessage[]) =>
    [...transcriptWork([message("ask", "user", [text("Go")]), ...replies]).turns.values()][0]
      ?.outcome;
  const card = () => message(`card-${seq}`, "bot", [helperBlock(`h-${seq}`)]);
  expect(outcome(card(), message("r", "bot", [text("Done.")]))).toBe("done");
  // A Helper or a file is a reply too: no text is not a Stop (the work state says that).
  expect(outcome(card())).toBe("done");
  expect(
    outcomeOf([
      message("f", "bot", [
        { kind: "file", artifactId: "a1", name: "deck.html", mimeType: "text/html", size: 1 },
      ]),
    ]),
  ).toBe("done");
  // Only a reply that left nothing but live status was cut short.
  expect(outcomeOf([])).toBe("cancelled");
  expect(outcomeOf([message("p", "bot", [{ kind: "progress", text: "Reading…" }])])).toBe(
    "cancelled",
  );
  expect(outcome(card(), message("e", "bot", [{ kind: "error", code: "overloaded" }]))).toBe(
    "failed",
  );
  expect(
    outcome(
      card(),
      message("q", "bot", [
        { kind: "reply_card", card: "ask", data: { question: "Which?" }, fallback: "Which?" },
      ]),
      message("r", "bot", [text("Which theme do you want?")]),
    ),
  ).toBe("asked");
});

it("takes the reply's Activity and the files its turn saved", () => {
  const file: MessageBlock = {
    kind: "reply_card",
    card: "file",
    id: "artifact:a1",
    data: { name: "acme.deck.html", artifactId: "a1" },
    fallback: "Saved",
  };
  const work = transcriptWork([
    message("ask", "user", [text("Make the deck")], 0),
    { ...message("started", "bot", [helperBlock("h1")], 2), activityId: "turn:conv:r1" },
    message("saved", "bot", [file], 3),
    { ...message("reply", "bot", [text("Here it is.")], 4), activityId: "turn:conv:r1" },
  ]);
  const turn = work.turns.get("reply");
  expect(turn?.activityId).toBe("turn:conv:r1");
  expect(turn?.files).toEqual(["a1"]);
  const none = transcriptWork([
    message("ask2", "user", [text("Again")], 0),
    message("started2", "bot", [helperBlock("h2")], 1),
  ]);
  const plain = [...none.turns.values()][0];
  expect([plain?.activityId, plain?.files]).toEqual([null, []]);
});

it("leaves the work from the first Round on to the Rounds' strips, which draw its Helpers", () => {
  const ask = message("ask", "user", [text("Make the deck")], 10);
  const messages = withRounds(
    [
      message("ask-0", "user", [text("Read the notes")], 0),
      message("reply-0", "bot", [text("Read."), helperBlock("h0")], 1),
      ask,
      message("card", "bot", [helperBlock("h2")], 11),
      // Steered into the running Round: no Round of its own.
      message("steer", "user", [text("Use the blue theme")], 12),
      message("reply", "bot", [text("Building it in blue."), helperBlock("h1")], 13),
    ],
    new Set(["ask"]),
  );
  expect(messages.map((m) => m.roundId ?? null)).toEqual([null, null, "ask", null, null, null]);
  const work = transcriptWork(messages);
  // Only the reply from before Rounds reads its own strip, and it is not the running one.
  expect([...work.turns.keys()]).toEqual(["reply-0"]);
  expect(work.turns.get("reply-0")?.last).toBe(false);
  expect(work.runningSince).toBeNull();
  expect([...work.drawn]).toEqual(["card:0", "reply:1", "reply-0:1"]);
  expect([...drawnOnlyMessageIds(messages, work)]).toEqual(["card"]);
  // No Round known: the same messages read as before.
  expect(withRounds([ask], new Set())).toEqual([ask]);
});
