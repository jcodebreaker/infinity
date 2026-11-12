import { describe, expect, it } from "vitest";
import { type OrbHome, orbPlacement } from "./placement";

const HOMES: OrbHome[] = ["hero", "sidebar", "toolbar"];

describe("orbPlacement", () => {
  it("puts the orb in the sidebar while the sidebar shows", () => {
    expect(orbPlacement({ startPage: false, sidebarVisible: true })).toBe("sidebar");
  });

  it("falls back to the toolbar when the sidebar is collapsed or hidden", () => {
    expect(orbPlacement({ startPage: false, sidebarVisible: false })).toBe("toolbar");
  });

  it("keeps the big orb on the start page, whatever the sidebar does", () => {
    expect(orbPlacement({ startPage: true, sidebarVisible: true })).toBe("hero");
    expect(orbPlacement({ startPage: true, sidebarVisible: false })).toBe("hero");
  });

  it("names exactly one home for every combination", () => {
    for (const startPage of [true, false]) {
      for (const sidebarVisible of [true, false]) {
        const home = orbPlacement({ startPage, sidebarVisible });
        expect(HOMES.filter((slot) => slot === home)).toHaveLength(1);
      }
    }
  });
});
