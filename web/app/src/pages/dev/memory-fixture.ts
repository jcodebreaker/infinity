import type { DailyNote } from "../../features/daily-notes";
import type { MemoryClaim } from "../../features/memory";

export const DEV_MEMORY_BOT_ID = "dev-memory-muse";

const at = (month: number, day: number) => Date.UTC(2026, month - 1, day, 15) / 1000;

/** One realistic claim of every kind, in the engine's own (third-person) wording. */
export const DEV_CLAIMS: MemoryClaim[] = [
  {
    id: "f1",
    kind: "fact",
    text: "Your favourite fruit is mango.",
    origin: "said",
    personAuthored: false,
    date: at(10, 4),
    quote: "mango is easily my favourite fruit",
  },
  {
    id: "f2",
    kind: "fact",
    text: "The user is a relationship manager at a retail bank.",
    origin: "said",
    personAuthored: false,
    date: at(10, 3),
  },
  {
    id: "p1",
    kind: "preference",
    text: "You prefer short bullet answers when planning.",
    origin: "said",
    personAuthored: false,
    date: at(10, 4),
    quote: "keep it to a few bullets when we plan",
  },
  {
    id: "i1",
    kind: "instruction",
    text: "You want weekly reports on AI agent product launches delivered every Monday morning in chat.",
    origin: "edited",
    personAuthored: true,
    date: at(10, 2),
  },
  {
    id: "c1",
    kind: "commitment",
    text: "You will send Dana the Q3 steering deck by Thu, Oct 8.",
    origin: "said",
    personAuthored: false,
    date: at(10, 4),
  },
  {
    id: "c2",
    kind: "commitment",
    text: "You promised to review the branch KPI pack by Oct 1.",
    origin: "said",
    personAuthored: false,
    date: at(9, 28),
  },
  {
    id: "c3",
    kind: "commitment",
    text: "You will book a follow-up with the compliance team.",
    origin: "noticed",
    personAuthored: false,
    date: at(10, 1),
  },
  {
    id: "j1",
    kind: "project",
    text: "The user is focused on the Q3 steering deck and the approvals workflow rollout.",
    origin: "said",
    personAuthored: false,
    date: at(10, 3),
  },
  {
    id: "j2",
    kind: "decision",
    text: "You decided to run the approvals pilot with two branches first.",
    origin: "said",
    personAuthored: false,
    date: at(10, 2),
  },
  {
    id: "j3",
    kind: "project",
    text: "The person needs to resolve whether the pilot reports to Operations or Risk.",
    origin: "noticed",
    personAuthored: false,
    date: at(10, 4),
  },
  {
    id: "j4",
    kind: "project",
    text: "Should the KPI summary also go to regional leads?",
    origin: "noticed",
    personAuthored: false,
    date: at(10, 3),
  },
  {
    id: "h1",
    kind: "person",
    text: "Dana Okafor is your manager; she owns the Q3 budget review and prefers async updates.",
    origin: "said",
    personAuthored: false,
    date: at(10, 3),
  },
  {
    id: "h2",
    kind: "person",
    text: "Maya Chen (Platform lead): runs the approvals pilot.",
    origin: "said",
    personAuthored: false,
    date: at(10, 1),
  },
  {
    id: "w1",
    kind: "working_style",
    text: "You like Superchat to ask one clarifying question before starting big tasks.",
    origin: "said",
    personAuthored: false,
    date: at(10, 2),
  },
  {
    id: "w2",
    kind: "working_style",
    text: "The user prefers phone-first slide decks.",
    origin: "said",
    personAuthored: false,
    date: at(9, 30),
  },
];

export const DEV_NOTES: DailyNote[] = [
  {
    date: "2026-10-04",
    sections: {
      talked_about: "Q3 steering deck and who the approvals pilot reports to.",
      decisions: "Start the pilot with two branches.",
      promised: "Send Dana the deck by Wednesday.",
      open_loops: "Operations or Risk as the pilot owner?",
    },
    editedByPerson: false,
    finalized: false,
    updatedAt: at(10, 4),
  },
  {
    date: "2026-10-03",
    sections: { talked_about: "Weekly KPI summary format." },
    editedByPerson: false,
    finalized: true,
    updatedAt: at(10, 3),
  },
];
