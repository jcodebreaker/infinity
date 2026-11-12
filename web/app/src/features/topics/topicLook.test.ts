import type { Topic } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import {
  featureTopic,
  firstSentences,
  openStatus,
  pickRows,
  TOPIC_PAIRS,
  topicHues,
  topicPalette,
  topicStatus,
  topicTag,
  workAllText,
} from "./topicLook";

const NOW = new Date("2026-10-25T10:00:00");

function topic(key: string, extra: Partial<Topic> = {}): Topic {
  return {
    key,
    title: "Polaris overview deck",
    leftOff: "",
    covered: [],
    open: [],
    starters: [],
    events: [],
    sources: {},
    since: null,
    sinceAt: null,
    lastTouchedAt: NOW.getTime() / 1000,
    ...extra,
  };
}

describe("topicLook", () => {
  it("gives each key a curated pair, and never two keys the same while pairs are free", () => {
    const keys = ["egypt", "lima", "morocco", "polaris", "q3-plan"];
    const palette = topicPalette(keys);
    expect(new Set(keys.map((key) => TOPIC_PAIRS.indexOf(palette.get(key)!))).size).toBe(5);
    // The same whatever order the topics are listed in.
    expect(topicPalette([...keys].reverse())).toEqual(palette);
    expect(TOPIC_PAIRS).toContainEqual(topicHues("unlisted"));
  });

  it("tags a topic with the writer's tag, else the first word that names it", () => {
    expect(topicTag({ tag: "Morocco", title: "Landing pages" })).toBe("Morocco");
    expect(topicTag({ tag: null, title: "The Polaris overview deck" })).toBe("Polaris");
    expect(topicTag({ tag: null, title: "Q3 plan review" })).toBe("Q3 plan");
  });

  it("says the status line, else the next open promise, else that it is done", () => {
    expect(topicStatus(topic("a", { statusLine: "3 slides need your eye" }), NOW)).toMatchObject({
      kind: "line",
      text: "3 slides need your eye",
    });
    const open = topic("b", {
      open: [{ kind: "Promised", text: "Send the link", due: "2026-10-27" }],
    });
    expect(topicStatus(open, NOW)).toEqual({
      kind: "open",
      text: "Send the link",
      due: "2026-10-27",
      needsYou: true,
    });
    expect(topicStatus(topic("c"), NOW)).toEqual({ kind: "done", needsYou: false });
  });

  it("leads with an unopened draft, else a promise due today or past, else nothing", () => {
    const due = topic("due", { open: [{ kind: "Promised", text: "Send it", due: "2026-10-25" }] });
    const later = topic("later", { open: [{ kind: "Open", text: "Ask", due: "2026-10-28" }] });
    const drafted = topic("drafted", { prepared: { itemId: "m1", artifactId: "f1", at: 1 } });
    expect(featureTopic([due, later, drafted], NOW)?.topic.key).toBe("drafted");
    expect(featureTopic([later, due], NOW)).toMatchObject({ why: "due", topic: { key: "due" } });
    expect(featureTopic([later], NOW)).toBeNull();
  });

  it("stops leading with a reason the person already worked on, and says where it stands", () => {
    const today = NOW.getTime() / 1000;
    const worked = topic("worked", {
      open: [{ kind: "Promised", text: "Send Dana the link", due: "2026-10-25" }],
      statusLine: "Phone check done; 3 layout fixes next",
      workedAt: today,
    });
    const other = topic("other", {
      open: [{ kind: "Promised", text: "Send it", due: "2026-10-24" }],
    });
    const drafted = topic("drafted", {
      prepared: { itemId: "m1", artifactId: "f1", at: today - 60 },
      workedAt: today,
    });
    expect(featureTopic([worked, drafted, other], NOW)?.topic.key).toBe("other");
    expect(featureTopic([worked, drafted], NOW)).toBeNull();
    expect(topicStatus(worked, NOW)).toMatchObject({
      kind: "line",
      text: "Phone check done; 3 layout fixes next",
      due: "2026-10-25",
    });
  });

  it("words an open line in full, dropping only its due words when the whole is long", () => {
    expect(openStatus("Send the link", "due today")).toBe("Send the link · due today");
    const long = "Fix the must-fix items on slides 6, 7 and 9 before the review call";
    expect(openStatus(long, "due today")).toBe(long);
  });

  it("keeps whole sentences only", () => {
    const text = "I checked all three pages. I drafted your note to Dana. Then a long tail.";
    expect(firstSentences(text, 60)).toBe(
      "I checked all three pages. I drafted your note to Dana.",
    );
  });

  it("leads the pick-up card with the line due first, pairing loose starters by shared words", () => {
    const { next, also } = pickRows(
      {
        open: [
          { kind: "Open", text: "Keep or remove the 'Plan your journey' section?" },
          { kind: "Open", text: "Fix the phone issues: no menu, small labels", due: "2026-10-28" },
          { id: "a1", kind: "Promised", text: "Send Dana the link", due: "2026-10-25" },
        ],
        starters: [
          "Fix the three phone issues on the landing page",
          "Remove the Plan your journey section",
          "Book the venue",
        ],
      },
      NOW,
    );
    expect(next).toMatchObject({ key: "a1", starter: null, due: "2026-10-25" });
    expect(also.map((row) => [row.kind, row.starter])).toEqual([
      ["Open", "Remove the Plan your journey section"],
      ["Open", "Fix the three phone issues on the landing page"],
      [null, "Book the venue"],
    ]);
    // "Work through all" sends every row as a plain line the person can read.
    expect(
      workAllText("Work through everything still open on Egypt", [next!, ...also]),
    ).toBe(`Work through everything still open on Egypt:
- Send Dana the link (promised, due 2026-10-25)
- Keep or remove the 'Plan your journey' section?
- Fix the phone issues: no menu, small labels (due 2026-10-28)
- Book the venue`);
    // A line's own starter wins, and the same loose starter is not shown twice.
    const own = pickRows(
      {
        open: [{ kind: "Open", text: "Pick a date", starter: "Book the venue", verb: "Book" }],
        starters: ["Book the venue"],
      },
      NOW,
    );
    expect(own).toEqual({
      next: {
        key: "Open:Pick a date",
        id: null,
        kind: "Open",
        text: "Pick a date",
        due: null,
        starter: "Book the venue",
        verb: "Book",
      },
      also: [],
    });
  });
});
