import { describe, expect, it } from "vitest";
import {
  drawnOrbEnergy,
  orbEnergyTarget,
  orbPixelSize,
  orbStateFor,
  stepOrbEnergy,
} from "./orbState";

describe("orb state", () => {
  it("brightens while Superchat thinks or works, and rests when idle or waiting on the person", () => {
    expect(orbStateFor("thinking")).toBe("working");
    expect(orbStateFor("working")).toBe("working");
    expect(orbStateFor("idle")).toBe("idle");
    expect(orbStateFor("waiting")).toBe("idle");
    expect(orbStateFor(undefined)).toBe("idle");
  });

  it("eases energy toward the state's target without overshooting", () => {
    expect(orbEnergyTarget("working")).toBe(1);
    expect(orbEnergyTarget("idle")).toBe(0);
    const step = stepOrbEnergy(0, "working", 0.1);
    expect(step).toBeGreaterThan(0);
    expect(step).toBeLessThan(1);
    expect(stepOrbEnergy(0, "working", 10)).toBe(1);
    expect(stepOrbEnergy(1, "idle", 10)).toBe(0);
  });

  it("keeps small orbs alive with an energy floor", () => {
    expect(drawnOrbEnergy(0, 22)).toBe(0.35);
    expect(drawnOrbEnergy(0.8, 22)).toBe(0.8);
    expect(drawnOrbEnergy(0, 96)).toBe(0);
  });

  it("caps the device pixel ratio at 2", () => {
    expect(orbPixelSize(34, 1)).toBe(34);
    expect(orbPixelSize(34, 2)).toBe(68);
    expect(orbPixelSize(34, 3)).toBe(68);
    expect(orbPixelSize(34, undefined)).toBe(34);
  });
});
