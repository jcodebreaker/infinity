// The Muse edition's bundled 3D illustrations (Microsoft Fluent Emoji, MIT; see NOTICE).
// This module resolves a key to where its PNG is served from.

/** Every bundled illustration key. */
export const ILLUSTRATION_KEYS = [
  "bank",
  "briefcase",
  "chart-increasing",
  "bar-chart",
  "money-bag",
  "coin",
  "credit-card",
  "dollar-banknote",
  "light-bulb",
  "spiral-calendar",
  "spiral-notepad",
  "clipboard",
  "books",
  "graduation-cap",
  "magnifying-glass",
  "envelope",
  "handshake",
  "laptop",
  "globe",
  "newspaper",
  "bell",
  "trophy",
  "rocket",
  "shield",
  "speech-balloon",
  "house",
] as const;
export type IllustrationKey = (typeof ILLUSTRATION_KEYS)[number];

/** Static public path for a bundled illustration (web/app/public/illustrations). */
export function illustrationUrl(key: IllustrationKey): string {
  return `/illustrations/${key}.png`;
}
