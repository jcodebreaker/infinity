import { describe, expect, it } from "vitest";
import {
  addressMemoryItem,
  commitmentLabel,
  isOpenQuestion,
  memoryLabel,
  parseDue,
  parseMemoryProfile,
  parsePerson,
  questionLabel,
} from "./memoryProfile";

const SAMPLE = [
  "[Standing memory about the user — provided by the system, not a message from the user]",
  "",
  "Preferences:",
  "- Prefers phone-first slide decks",
  "- Weekly summaries on Mondays",
  "About the user:",
  "- Works branch operations at a bank",
  "",
  "[End of standing memory]",
].join("\n");

it("drops the bracketed framing lines", () => {
  const sections = parseMemoryProfile(SAMPLE);
  const text = sections.flatMap((section) => [section.heading, ...section.items]).join(" ");
  expect(text).not.toContain("[");
  expect(text).not.toContain("]");
});

it("groups bullet items under their heading, in order", () => {
  const sections = parseMemoryProfile(SAMPLE);
  expect(sections).toEqual([
    {
      heading: "Preferences",
      items: ["Prefers phone-first slide decks", "Weekly summaries on Mondays"],
    },
    { heading: "About the user", items: ["Works branch operations at a bank"] },
  ]);
});

it("returns an empty list for null, undefined, or blank input", () => {
  expect(parseMemoryProfile(null)).toEqual([]);
  expect(parseMemoryProfile(undefined)).toEqual([]);
  expect(parseMemoryProfile("   \n  ")).toEqual([]);
});

it("keeps stray bullet items under an empty heading instead of dropping them", () => {
  const sections = parseMemoryProfile("- A note with no heading above it");
  expect(sections).toEqual([{ heading: "", items: ["A note with no heading above it"] }]);
});

it("drops an empty section (a heading with no items under it)", () => {
  const sections = parseMemoryProfile("Preferences:\nAbout the user:\n- Works at a bank");
  expect(sections).toEqual([{ heading: "About the user", items: ["Works at a bank"] }]);
});

describe("addressMemoryItem", () => {
  it.each([
    ["The user's favourite fruit is mango.", "Your favourite fruit is mango."],
    ["User's team is Platform.", "Your team is Platform."],
    ["The user is a relationship manager.", "You are a relationship manager."],
    [
      "This month the user is focused on agent safety and the user's approvals work.",
      "This month you're focused on agent safety and your approvals work.",
    ],
    ["The user has two direct reports.", "You have two direct reports."],
    ["The user prefers short answers.", "You prefer short answers."],
    ["The user doesn't like long emails.", "You don't like long emails."],
    ["The user watches the markets daily.", "You watch the markets daily."],
    ["Lives in Toronto.", "Lives in Toronto."],
    ["The username is sam.", "The username is sam."],
    ["The user is a PM and is a product manager.", "You are a PM and are a product manager."],
    [
      "The user is building an AI assistant and is a product manager.",
      "You are building an AI assistant and are a product manager.",
    ],
  ])("%s", (input, expected) => {
    expect(addressMemoryItem(input)).toBe(expected);
  });
});

describe("memoryLabel", () => {
  it.each([
    [
      "You prefer short bullet answers when planning.",
      "Prefers short bullet answers when planning",
    ],
    ["Your favourite fruit is mango.", "Favourite fruit: mango"],
    ["The user's team is Platform.", "Team: Platform"],
    ["The user prefers phone-first slide decks.", "Prefers phone-first slide decks"],
    [
      "The user is a relationship manager at a retail bank.",
      "Relationship manager at a retail bank",
    ],
    ["You're focused on agent safety.", "Focused on agent safety"],
    ["You have two direct reports.", "Has two direct reports"],
    ["The user doesn't like long emails.", "Doesn't like long emails"],
    ["You watch the markets daily.", "Watches the markets daily"],
    ["User wants weekly reports.", "Wants weekly reports"],
    ["The person needs to resolve whether X.", "Needs to resolve whether X"],
    ["Lives in Toronto.", "Lives in Toronto"],
    ["The username is sam.", "The username is sam"],
  ])("%s", (input, expected) => {
    expect(memoryLabel(input)).toBe(expected);
  });

  it("is deterministic and never empty for non-empty input", () => {
    expect(memoryLabel("You")).toBe("You");
    expect(memoryLabel("  ")).toBe("");
  });
});

describe("questions and commitments", () => {
  it("spots open questions", () => {
    expect(isOpenQuestion("Should the KPI summary go to leads?")).toBe(true);
    expect(isOpenQuestion("The person needs to resolve whether A or B.")).toBe(true);
    expect(isOpenQuestion("You decided to start with two branches.")).toBe(false);
  });

  it("labels a question as the task", () => {
    expect(questionLabel("The person needs to resolve whether A or B.")).toBe(
      "Resolve whether A or B",
    );
  });

  it("labels a commitment as the action", () => {
    expect(commitmentLabel("You will send Dana the deck.")).toBe("Send Dana the deck");
    expect(commitmentLabel("You promised to review the pack.")).toBe("Review the pack");
  });
});

describe("parseDue", () => {
  const now = new Date(2026, 9, 4, 12);
  it("extracts an upcoming date and strips it from the text", () => {
    expect(parseDue("You will send Dana the deck by Thu, Oct 8.", now)).toEqual({
      text: "You will send Dana the deck.",
      due: { label: "Thu, Oct 8", overdue: false },
    });
  });
  it("marks a past date overdue", () => {
    expect(parseDue("Review the pack due Oct 1", now).due).toEqual({
      label: "Thu, Oct 1",
      overdue: true,
    });
  });
  it("today is not overdue; no date means no chip", () => {
    expect(parseDue("Call back on Oct 4", now).due?.overdue).toBe(false);
    expect(parseDue("Book a follow-up", now)).toEqual({ text: "Book a follow-up", due: null });
  });
});

describe("parsePerson", () => {
  it("splits name, relation and facts", () => {
    expect(parsePerson("Dana Okafor is your manager; she owns the Q3 review.")).toEqual({
      name: "Dana Okafor",
      relation: "manager",
      facts: "Owns the Q3 review",
    });
    expect(parsePerson("Maya Chen (Platform lead): runs the pilot.")).toEqual({
      name: "Maya Chen",
      relation: "Platform lead",
      facts: "Runs the pilot",
    });
    expect(parsePerson("Maya")).toEqual({ name: "Maya", relation: null, facts: null });
  });
});
