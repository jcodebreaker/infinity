import { describe, expect, it } from "vitest";

import { localTimezone } from "./local-timezone.js";

describe("localTimezone", () => {
  it("returns a non-empty IANA timezone string", () => {
    const zone = localTimezone();
    expect(zone).not.toBe("");
    expect(zone).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  });
});

describe("friendlyTimezone / formatClockTime", () => {
  it("labels a zone with its city and generic name", async () => {
    const { friendlyTimezone } = await import("./local-timezone.js");
    expect(friendlyTimezone("America/Toronto", "en-US")).toBe("Toronto (Eastern Time)");
    expect(friendlyTimezone("America/Argentina/Buenos_Aires", "en-US")).toContain("Buenos Aires");
  });

  it("formats 12-hour times without a leading zero", async () => {
    const { formatClockTime } = await import("./local-timezone.js");
    expect(formatClockTime("08:00", "en-US")).toBe("8:00 AM");
    expect(formatClockTime("22:00", "en-US")).toBe("10:00 PM");
    expect(formatClockTime("00:30", "en-US")).toBe("12:30 AM");
  });
});
