import { describe, expect, it } from "vitest";
import { museBotProfile } from "./muse.js";

describe("museBotProfile", () => {
  it("titles the Muse an AI teammate, not an executive assistant", () => {
    const profile = museBotProfile("Superchat", "Jamie");
    expect(profile.title).toBe("AI teammate");
    expect(profile.title.toLowerCase()).not.toContain("assistant");
    expect(profile.description.toLowerCase()).not.toContain("assistant");
    expect(profile.instructions.toLowerCase()).not.toContain("assistant");
  });

  it("names the Muse and the person in the description and instructions", () => {
    const profile = museBotProfile("Superchat", "Jamie");
    expect(profile.description).toContain("Jamie");
    expect(profile.instructions).toContain("Superchat");
    expect(profile.instructions).toContain("Jamie");
  });

  it("falls back to a generic person description when no name is given", () => {
    const profile = museBotProfile("Superchat", "");
    expect(profile.description).toContain("the person you work for");
  });
});
