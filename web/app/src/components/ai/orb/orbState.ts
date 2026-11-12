import type { MuseState } from "@superchat/contracts";

/** What an orb shows: calm, or brighter and faster while Superchat works. */
export type OrbState = "idle" | "working";

/** Device pixels are capped at 2: past that the shader costs more than anyone can see. */
export const ORB_MAX_DPR = 2;
/** Below this size an orb never fully dims, so a tiny one still reads as alive. */
export const ORB_SMALL_PX = 40;
const SMALL_ORB_ENERGY_FLOOR = 0.35;
/** How fast energy eases toward its target, per second (the mockup's 2.5). */
const ENERGY_EASE = 2.5;

/** Superchat's live state as an orb state: thinking and working brighten it; idle and waiting rest. */
export function orbStateFor(state: MuseState | undefined): OrbState {
  return state === "thinking" || state === "working" ? "working" : "idle";
}

export function orbEnergyTarget(state: OrbState): number {
  return state === "working" ? 1 : 0;
}

/** One eased step of an orb's energy toward its state's target. */
export function stepOrbEnergy(current: number, state: OrbState, dtSeconds: number): number {
  const target = orbEnergyTarget(state);
  return current + (target - current) * Math.min(1, dtSeconds * ENERGY_EASE);
}

/** The energy the shader draws with: small orbs keep a floor so they never look switched off. */
export function drawnOrbEnergy(energy: number, sizePx: number): number {
  return sizePx < ORB_SMALL_PX ? Math.max(energy, SMALL_ORB_ENERGY_FLOOR) : energy;
}

/** An orb's backing-store size in device pixels. */
export function orbPixelSize(size: number, devicePixelRatio: number | undefined): number {
  return Math.max(1, Math.round(size * Math.min(ORB_MAX_DPR, devicePixelRatio || 1)));
}
