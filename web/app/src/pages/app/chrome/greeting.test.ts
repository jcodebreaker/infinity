import { describe, expect, it } from "vitest";
import { greetingForHour, greetingLead } from "./greeting";

describe("greetingForHour", () => {
  it("is morning from 5 to 11", () => {
    expect(greetingForHour(5)).toBe("Good morning");
    expect(greetingForHour(11)).toBe("Good morning");
  });

  it("is afternoon from 12 to 17", () => {
    expect(greetingForHour(12)).toBe("Good afternoon");
    expect(greetingForHour(17)).toBe("Good afternoon");
  });

  it("is evening otherwise, including past midnight", () => {
    expect(greetingForHour(18)).toBe("Good evening");
    expect(greetingForHour(23)).toBe("Good evening");
    expect(greetingForHour(0)).toBe("Good evening");
    expect(greetingForHour(4)).toBe("Good evening");
  });
});

describe("greetingLead", () => {
  it("greets the person by name at the right time of day", () => {
    expect(greetingLead(new Date("2026-09-27T08:00:00"), "Alex")).toBe("Good morning, Alex.");
    expect(greetingLead(new Date("2026-09-27T15:00:00"), "Alex")).toBe("Good afternoon, Alex.");
    expect(greetingLead(new Date("2026-09-27T20:00:00"), "Alex")).toBe("Good evening, Alex.");
  });

  it("drops the name when it is blank", () => {
    expect(greetingLead(new Date("2026-09-27T08:00:00"), "")).toBe("Good morning.");
  });

  it("drops a placeholder or a login id: only a real first name is used", () => {
    expect(greetingLead(new Date("2026-09-27T08:00:00"), "local")).toBe("Good morning.");
    expect(greetingLead(new Date("2026-09-27T08:00:00"), "jdoe42")).toBe("Good morning.");
    expect(greetingLead(new Date("2026-09-27T08:00:00"), "Alex Kim")).toBe("Good morning, Alex.");
  });
});
