import { DEFAULT_MUSE_COLOR } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { bloopPalette, NEUTRAL_POSE, poseFor, shade } from "./expression";

const STATES = ["idle", "thinking", "working", "waiting"] as const;

describe("bloop expressions", () => {
  it("gives every Muse state a pose with all the same keys, all finite", () => {
    for (const state of STATES) {
      const pose = poseFor(state, 1.3);
      expect(Object.keys(pose).sort()).toEqual(Object.keys(NEUTRAL_POSE).sort());
      for (const value of Object.values(pose)) expect(Number.isFinite(value)).toBe(true);
    }
  });

  it("makes each state readable from the face alone", () => {
    const idle = poseFor("idle", 1);
    const thinking = poseFor("thinking", 1);
    const working = poseFor("working", 1);
    const waiting = poseFor("waiting", 1);

    // Idle follows the pointer; thinking looks away, up, with a raised hand and a thought cloud.
    expect(idle.follow).toBe(1);
    expect(thinking.follow).toBe(0);
    expect(thinking.gy).toBeGreaterThan(0.5);
    expect(thinking.pThink).toBe(1);
    // Working squints and frowns with focus and shows the spinner.
    expect(working.eye).toBeLessThan(idle.eye);
    expect(working.bTL).toBeGreaterThan(0);
    expect(working.pSpin).toBe(1);
    // Waiting looks hopeful (worried brows, open eyes) and shows the "…" bubble.
    expect(waiting.bTL).toBeLessThan(0);
    expect(waiting.eye).toBeGreaterThan(idle.eye);
    expect(waiting.pChat).toBe(1);
  });

  it("shows only the prop that belongs to the state", () => {
    expect(poseFor("idle", 1)).toMatchObject({ pThink: 0, pSpin: 0, pChat: 0 });
    expect(poseFor("thinking", 1)).toMatchObject({ pThink: 1, pSpin: 0, pChat: 0 });
    expect(poseFor("working", 1)).toMatchObject({ pThink: 0, pSpin: 1, pChat: 0 });
    expect(poseFor("waiting", 1)).toMatchObject({ pThink: 0, pSpin: 0, pChat: 1 });
  });

  it("waves as soon as the Muse starts waiting", () => {
    expect(poseFor("waiting", 0.2).aR).toBeGreaterThan(2);
    expect(poseFor("waiting", 3).aR).toBeLessThan(1);
  });

  it("scales bodily motion down for reduced motion without changing the expression", () => {
    const full = poseFor("working", 0.4, 1);
    const calm = poseFor("working", 0.4, 0.35);
    expect(calm.eye).toBe(full.eye);
    expect(calm.bTL).toBe(full.bTL);
    expect(Math.abs(calm.squash)).toBeLessThanOrEqual(Math.abs(full.squash));
  });
});

describe("bloop palette", () => {
  const lightness = (hex: string) => {
    const n = Number.parseInt(hex.slice(1), 16);
    const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => v / 255) as [
      number,
      number,
      number,
    ];
    return (Math.max(r, g, b) + Math.min(r, g, b)) / 2;
  };

  it("gives a light, soft palette: airy top, lighter-still rim, a mid bottom", () => {
    const { top, bot, rim, core } = bloopPalette(DEFAULT_MUSE_COLOR);
    for (const hex of [top, bot, rim, core]) expect(hex).toMatch(/^#[0-9a-f]{6}$/);
    expect(lightness(rim)).toBeGreaterThan(lightness(top));
    expect(lightness(top)).toBeGreaterThan(lightness(core));
    expect(lightness(core)).toBeGreaterThan(lightness(bot));
    expect(lightness(bot)).toBeGreaterThan(0.55);
  });

  it("lands vivid and very dark identity colors on the same soft lightness", () => {
    const vivid = bloopPalette("#0090FF");
    const dark = bloopPalette("#0A1F5C");
    expect(Math.abs(lightness(vivid.bot) - lightness(dark.bot))).toBeLessThan(0.02);
    expect(Math.abs(lightness(vivid.top) - lightness(dark.top))).toBeLessThan(0.02);
  });

  it("falls back to the Muse blue for white and grey, which have no hue", () => {
    expect(bloopPalette("#FFFFFF")).toEqual(bloopPalette(DEFAULT_MUSE_COLOR));
    expect(bloopPalette("#8A8A8A")).toEqual(bloopPalette(DEFAULT_MUSE_COLOR));
  });

  it("keeps the hue of a colored identity", () => {
    expect(bloopPalette("#F2B233").bot).not.toEqual(bloopPalette(DEFAULT_MUSE_COLOR).bot);
  });
});

describe("shade", () => {
  it("falls back to the Muse color for an invalid value instead of throwing", () => {
    expect(shade("not a color", 0.1)).toBe(shade(DEFAULT_MUSE_COLOR, 0.1));
  });

  it("keeps greys grey and clamps extreme lightness", () => {
    const grey = shade("#808080", 0.1);
    expect(grey.slice(1, 3)).toBe(grey.slice(3, 5));
    expect(shade("#ffffff", 0.5)).toMatch(/^#[0-9a-f]{6}$/);
    expect(shade("#000000", -0.5)).toMatch(/^#[0-9a-f]{6}$/);
  });
});
