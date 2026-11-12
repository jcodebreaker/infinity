import type { MuseState } from "@superchat/contracts";
import { DEFAULT_MUSE_COLOR } from "@superchat/contracts";

// Bloop is the Muse's 3D face for the large moments (welcome, sign-in, onboarding). Every
// expression is a pose: a flat set of numbers the scene eases toward, so the four Muse states
// stay data and the renderer stays dumb. Pure functions only, so it runs without WebGL.

export interface BloopPose {
  /** Eye openness (1 = normal) and width. */
  eye: number;
  eyeW: number;
  /** Gaze, -1..1 each; `follow` blends from this gaze toward the pointer. */
  gx: number;
  gy: number;
  follow: number;
  /** Brow lift, and per-side lift / tilt (tilt > 0 is a frown, < 0 is worried). */
  bY: number;
  bRL: number;
  bRR: number;
  bTL: number;
  bTR: number;
  /** Mouth: curve (-1 frown .. 1 smile), openness, width, wobble; tongue tip out. */
  smile: number;
  open: number;
  mW: number;
  sq: number;
  tongue: number;
  blush: number;
  /** Body. */
  lean: number;
  tilt: number;
  squash: number;
  hop: number;
  aR: number;
  aL: number;
  xR: number;
  xL: number;
  fR: number;
  tipX: number;
  tipZ: number;
  wob: number;
  /** Props, eased in and out: thought cloud, work spinner, "…" bubble. */
  pThink: number;
  pSpin: number;
  pChat: number;
}

export const NEUTRAL_POSE: BloopPose = {
  eye: 1,
  eyeW: 1,
  gx: 0,
  gy: 0,
  follow: 0.8,
  bY: 0,
  bRL: 0,
  bRR: 0,
  bTL: 0,
  bTR: 0,
  smile: 0.45,
  open: 0,
  mW: 1,
  sq: 0,
  tongue: 0,
  blush: 0.8,
  lean: 0,
  tilt: 0,
  squash: 0,
  hop: 0,
  aR: 0.4,
  aL: 0.4,
  xR: 0,
  xL: 0,
  fR: 0,
  tipX: 0,
  tipZ: 0,
  wob: 0.02,
  pThink: 0,
  pSpin: 0,
  pChat: 0,
};

export const POSE_KEYS = Object.keys(NEUTRAL_POSE) as (keyof BloopPose)[];

/** Props fade in slower than the face moves; body keys react fast. */
export const PROP_KEYS: ReadonlySet<keyof BloopPose> = new Set(["pThink", "pSpin", "pChat"]);
export const FAST_KEYS: ReadonlySet<keyof BloopPose> = new Set([
  "hop",
  "squash",
  "tilt",
  "lean",
  "xR",
  "xL",
  "aR",
  "aL",
  "fR",
]);

/**
 * The pose for `state`, `t` seconds after it began. `amp` scales every bodily motion (1 normally,
 * lower for reduced motion) so the same expressions read as a calm still.
 */
export function poseFor(state: MuseState, t: number, amp = 1): BloopPose {
  const pose: BloopPose = { ...NEUTRAL_POSE };
  switch (state) {
    case "idle": {
      pose.follow = 1;
      pose.smile = 0.5;
      pose.squash = 0.03 * Math.sin(t * 1.8) * amp;
      pose.tilt = 0.02 * Math.sin(t * 0.9) * amp;
      const beat = t % 7;
      if (beat < 0.5) pose.hop = 0.18 * Math.sin((Math.PI * beat) / 0.5) * amp;
      pose.aR = 0.45 + 0.05 * Math.sin(t * 1.3);
      pose.aL = 0.45 + 0.05 * Math.sin(t * 1.3 + 1);
      break;
    }
    case "thinking": {
      const side = Math.floor(t / 1.7) % 2;
      pose.gx = side ? 0.6 : -0.75;
      pose.gy = 0.65;
      pose.follow = 0;
      pose.eye = 0.92;
      pose.bRL = 0.06;
      pose.bRR = -0.02;
      pose.bTR = 0.28;
      pose.smile = -0.05;
      pose.sq = 0.7;
      pose.mW = 0.55;
      pose.blush = 0.6;
      pose.tilt = 0.13 + 0.03 * Math.sin(t * 1.2) * amp;
      pose.tipX = 0.3;
      pose.aR = 2.3 + 0.05 * Math.sin(t * 2);
      pose.xR = -0.3;
      pose.squash = 0.02 * Math.sin(t * 2) * amp;
      pose.pThink = 1;
      break;
    }
    case "working": {
      pose.follow = 0;
      pose.gx = 0.06 * Math.sin(t * 2);
      pose.gy = t % 3.4 < 2.7 ? -0.75 : 0.15;
      pose.eye = 0.74;
      pose.bTL = 0.38;
      pose.bTR = 0.38;
      pose.bY = -0.015;
      pose.smile = 0.2;
      pose.open = 0.08;
      pose.mW = 0.5;
      pose.tongue = 1;
      pose.lean = 0.1;
      const left = Math.max(0, Math.sin(t * 15));
      const right = Math.max(0, Math.sin(t * 15 + Math.PI));
      pose.aR = 0.05;
      pose.aL = 0.05;
      pose.xR = -1.25 + 0.26 * right * amp;
      pose.xL = -1.25 + 0.26 * left * amp;
      pose.squash = 0.015 * Math.sin(t * 14) * amp;
      pose.wob = 0.035;
      pose.pSpin = 1;
      break;
    }
    case "waiting": {
      const gaze = t % 6;
      pose.gx = gaze < 2 ? 0 : gaze < 3 ? -0.7 : gaze < 4 ? 0.7 : 0;
      pose.gy = 0.05 + (gaze >= 4 && gaze < 5 ? 0.45 : 0);
      pose.follow = 0.2;
      pose.eye = 1.05;
      pose.bTL = -0.35;
      pose.bTR = -0.35;
      pose.bY = 0.035;
      pose.smile = 0.25;
      pose.blush = 0.85;
      pose.mW = 0.8;
      pose.tilt = 0.07 * Math.sin(t * 1.1) * amp;
      pose.squash = 0.02 * Math.sin(t * 2.2) * amp;
      pose.fR = 0.1 * Math.max(0, Math.sin(t * 7)) * amp;
      pose.aL = 0.35;
      // A wave right away, then every few seconds.
      const wave = (t + 5.7) % 7;
      pose.aR = wave > 5.2 && wave < 6.6 ? 2.3 + 0.4 * Math.sin(t * 14) * amp : 0.4;
      pose.pChat = 1;
      break;
    }
  }
  return pose;
}

// ---- identity color -> jelly palette ----

interface Hsl {
  h: number;
  s: number;
  l: number;
}

function parseHex(hex: string): [number, number, number] {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  const digits = match?.[1] ?? DEFAULT_MUSE_COLOR.slice(1);
  const value = Number.parseInt(digits, 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function rgbToHsl([r, g, b]: [number, number, number]): Hsl {
  const rn = r / 255;
  const gn = g / 255;
  const bn = b / 255;
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const l = (max + min) / 2;
  if (max === min) return { h: 0, s: 0, l };
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h: number;
  if (max === rn) h = (gn - bn) / d + (gn < bn ? 6 : 0);
  else if (max === gn) h = (bn - rn) / d + 2;
  else h = (rn - gn) / d + 4;
  return { h: h / 6, s, l };
}

function hslToHex({ h, s, l }: Hsl): string {
  const channel = (n: number) => {
    const k = (n + h * 12) % 12;
    const a = s * Math.min(l, 1 - l);
    const v = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(v * 255)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${channel(0)}${channel(8)}${channel(4)}`;
}

/** `hex` with its lightness moved by `delta` (-1..1). Invalid input falls back to the Muse color. */
export function shade(hex: string, delta: number): string {
  const hsl = rgbToHsl(parseHex(hex));
  return hslToHex({ ...hsl, l: Math.min(0.92, Math.max(0.08, hsl.l + delta)) });
}

export interface BloopPalette {
  top: string;
  bot: string;
  rim: string;
  core: string;
}

/**
 * A light, candy-soft jelly palette from the identity color's hue alone. Lightness is fixed so
 * every identity color reads soft and cute (a deep navy or a vivid blue both land on the same
 * airy tones), and greys and whites, which have no hue, fall back to the Muse blue.
 */
export function bloopPalette(hex: string): BloopPalette {
  let hsl = rgbToHsl(parseHex(hex));
  if (hsl.s < 0.18) hsl = rgbToHsl(parseHex(DEFAULT_MUSE_COLOR));
  const s = Math.min(hsl.s, 0.9);
  const at = (l: number, saturation: number) => hslToHex({ h: hsl.h, s: saturation, l });
  return {
    top: at(0.82, s * 0.85),
    bot: at(0.6, s),
    rim: at(0.9, s * 0.7),
    core: at(0.7, s * 0.9),
  };
}
