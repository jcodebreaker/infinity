import type { ReplyCardBlock, ThreadMessage } from "@superchat/contracts";

// Fixture data for /dev/clarify and /dev/followups (CardsPreviewPage.tsx): the cards
// `ask_clarification` and `suggest_follow_ups` produce, on a bank-workplace thread.

export type CardsScenario = "clarify" | "followups" | "looks";

let seq = 0;
function message(role: "user" | "bot", ...blocks: ThreadMessage["blocks"]): ThreadMessage {
  seq += 1;
  return {
    id: `cards-${seq}`,
    threadId: "cards",
    seq,
    role,
    blocks,
    createdAt: new Date(Date.now() - (60 - seq) * 60_000).toISOString(),
  };
}

const text = (value: string): ThreadMessage["blocks"][number] => ({ kind: "text", text: value });

export function clarificationCard(question: string, options: string[]): ReplyCardBlock {
  return {
    kind: "reply_card",
    card: "ask",
    data: { question, options: options.map((label, i) => ({ id: `opt-${i + 1}`, label })) },
    fallback: `${question}\n\n${options.map((o) => `- ${o}`).join("\n")}`,
  };
}

export function followUpsCard(suggestions: string[]): ReplyCardBlock {
  return {
    kind: "reply_card",
    card: "follow_ups",
    data: { suggestions },
    fallback: suggestions.map((s) => `- ${s}`).join("\n"),
  };
}

/** The three looks the Muse offers for a new deck: one professional, one editorial, one dark. */
export const LOOKS = [
  ["corporate-clean", "Corporate Clean"],
  ["editorial-serif", "Editorial Serif"],
  ["tokyo-night", "Tokyo Night"],
] as const;

function lookCard(): ReplyCardBlock {
  return {
    kind: "reply_card",
    card: "ask",
    data: {
      question: "Which look?",
      options: LOOKS.map(([id, label], i) => ({
        id: `opt-${i + 1}`,
        label,
        preview: { kind: "deck-theme", id },
      })),
    },
    fallback: `Which look?\n\n${LOOKS.map(([, label]) => `- ${label}`).join("\n")}`,
  };
}

/** The thread each route opens on. */
export function seedMessages(scenario: CardsScenario): ThreadMessage[] {
  seq = 0;
  if (scenario === "looks") {
    return [
      message("user", text("Can you build for me a deck for this?")),
      message("bot", lookCard()),
    ];
  }
  if (scenario === "clarify") {
    return [
      message("user", text("Pull the staffing numbers for the review.")),
      message(
        "bot",
        clarificationCard("Which branches should the staffing numbers cover?", [
          "All three branches",
          "Downtown only",
          "Downtown and Riverside",
        ]),
      ),
    ];
  }
  return [
    {
      ...message("user", text("How is the loan file audit backlog trending?")),
      // The chip of the artifact the message is about (docs/conversation.md).
      artifact: {
        artifactId: "art_dev_backlog",
        name: "Audit backlog.html",
        version: 3,
        how: "edited",
      },
    },
    message(
      "bot",
      text(
        "The backlog fell from 84 files to 61 over the last three weeks. Riverside cleared the most (19), while Downtown is flat at 24 because two auditors are out until the 20th.",
      ),
    ),
    message(
      "bot",
      followUpsCard([
        "Draft a note to Downtown's manager",
        "Show the backlog by auditor",
        "Compare with last quarter",
      ]),
    ),
  ];
}

/** What the Muse answers after the person's pick or chip, so the flow is visible end to end. */
export function replyTo(scenario: CardsScenario, said: string): ThreadMessage[] {
  if (scenario === "looks") {
    return [
      message("bot", text(`Here is the deck in ${said}.`)),
      message("bot", {
        kind: "reply_card",
        card: "file",
        data: {
          name: "q3-review.deck.html",
          artifactId: "dev-deck",
          kind: "html",
          size: 442_536,
          version: 1,
        },
        fallback: "q3-review.deck.html",
      }),
    ];
  }
  if (scenario === "clarify") {
    return [
      message(
        "bot",
        text(
          `Got it: ${said.toLowerCase()}. Headcount is 38 FTE with 3 open roles; attrition is 4% year to date.`,
        ),
      ),
    ];
  }
  return [
    message("bot", text(`Here is "${said}": drafted in your Library as a short note.`)),
    message("bot", followUpsCard(["Make it shorter", "Send it to me by email"])),
  ];
}
