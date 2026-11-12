export const APPEARANCE_PREFERENCES = ["system", "light", "dark"] as const;

export type AppearancePreference = (typeof APPEARANCE_PREFERENCES)[number];

export type ResolvedAppearance = "light" | "dark";

export const UI_APPEARANCE_STORAGE_KEY = "superchat.uiAppearance";

/**
 * Semantic palette shared by web, Electron, and Expo. Names follow the shadcn
 * convention so the same slot means the same thing on every surface: a `border`
 * is always a border and never a fill.
 */
export type ColorTokens = {
  background: string;
  foreground: string;
  card: string;
  cardForeground: string;
  popover: string;
  popoverForeground: string;
  primary: string;
  primaryForeground: string;
  secondary: string;
  secondaryForeground: string;
  chatUser: string;
  chatUserForeground: string;
  muted: string;
  mutedForeground: string;
  accent: string;
  accentForeground: string;
  destructive: string;
  destructiveForeground: string;
  border: string;
  input: string;
  ring: string;
  sidebar: string;
  sidebarForeground: string;
  sidebarBorder: string;
  sidebarAccent: string;
  sidebarAccentForeground: string;
  link: string;
  success: string;
  warning: string;
  overlay: string;
  scrollbar: string;
  scrollbarHover: string;
  /** Muse only: the ground the shell's panels float on. */
  ground?: string;
  /** Muse only: the ground's single soft radial wash, top right. */
  "ground-wash"?: string;
  /** Muse only: a floating panel's translucent fill (rail, chat, context panel). */
  panel?: string;
  /** Muse only: an opaque surface on a panel (composer, popovers, cards). */
  solid?: string;
  /** Muse only: every hairline: panel edges, dividers, the composer's border. */
  line?: string;
  /** Muse only: primary text. */
  ink?: string;
  /** Muse only: secondary text and icons. */
  "ink-2"?: string;
  /** Muse only: faint text: placeholders, counts, timestamps. */
  "ink-3"?: string;
  /** Muse only: the person's message bubble. */
  bubble?: string;
  /** Muse only: the selected or hovered row. */
  selection?: string;
  /** Muse only: the ground the content window sits on. */
  window?: string;
  /** Muse only: the rounded content window. */
  content?: string;
  /** Muse only: a floating glass panel's translucent fill (sidebar, inspector). */
  glass?: string;
  /** Muse only: a glass panel's half-pixel edge. */
  "glass-line"?: string;
  /** Muse only: an inset grouped list or card. */
  group?: string;
  /** Muse only: inset hairlines between rows. */
  separator?: string;
  /** Muse only: a soft accent fill. */
  "selection-strong"?: string;
  /** Muse only: the accent: the primary action, the selected row, the person's bubble. */
  tint?: string;
  /** Muse only: accent text on a soft accent fill. */
  "tint-ink"?: string;
  /** Muse only: the light-blue stop of the live glow (the work strip's edge while work runs). */
  "tint-2"?: string;
  /** Muse only: the violet stop of the live glow, and of the live orb. */
  "tint-3"?: string;
  /** Muse only: live / done dot. */
  ok?: string;
  /** Muse only: waiting dot. */
  warn?: string;
  /** Muse only: the red count badge. */
  alert?: string;
  /** Muse only: section signature color. */
  "sig-goals"?: string;
  /** Muse only: section signature color. */
  "sig-feed"?: string;
  /** Muse only: section signature color. */
  "sig-ideas"?: string;
  /** Muse only: section signature color. */
  "sig-library"?: string;
  /** Muse only: section signature color. */
  "sig-waiting"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-blue-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-blue-to"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-green-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-green-to"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-yellow-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-yellow-to"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-orange-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-orange-to"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-purple-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-purple-to"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-red-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-red-to"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-indigo-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-indigo-to"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-teal-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-teal-to"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-gray-from"?: string;
  /** Muse only: Apple tile gradient stop. */
  "tile-gray-to"?: string;
  /** Muse only: the dark result tile. */
  media?: string;
  /** Muse only: the dark result tile. */
  "media-deep"?: string;
  /** Muse only: the dark result tile. */
  "media-glow"?: string;
  /** Muse only: the dark result tile. */
  "media-bar"?: string;
  /** Muse only: the dark result tile. */
  "media-bar-2"?: string;
  /** Muse only: the dark result tile. */
  "media-key-from"?: string;
  /** Muse only: the dark result tile. */
  "media-key-to"?: string;
  /** Muse only: a Feed media tile's glow. */
  "media-warm-glow"?: string;
  /** Muse only: a Feed media tile's glow. */
  "media-warm-deep"?: string;
  /** Muse only: a Feed media tile's glow. */
  "media-pink-glow"?: string;
  /** Muse only: a Feed media tile's glow. */
  "media-pink-deep"?: string;
  /** Muse only: a Feed media tile's glow. */
  "media-green-glow"?: string;
  /** Muse only: a Feed media tile's glow. */
  "media-green-deep"?: string;
  /** Signed-out welcome page (light in both themes): the page. */
  "welcome-bg"?: string;
  /** Signed-out welcome page (light in both themes): the conversation card. */
  "welcome-paper"?: string;
  /** Signed-out welcome page (light in both themes): text. */
  "welcome-ink"?: string;
  /** Signed-out welcome page (light in both themes): secondary text. */
  "welcome-ink-2"?: string;
  /** Signed-out welcome page (light in both themes): tertiary text. */
  "welcome-ink-3"?: string;
  /** Signed-out welcome page (light in both themes): a hairline. */
  "welcome-line"?: string;
  /** Signed-out welcome page (light in both themes): the user bubble and accent. */
  "welcome-me"?: string;
  /** Signed-out welcome page (light in both themes): Superchat's bubble. */
  "welcome-bubble"?: string;
  /** Signed-out welcome page (light in both themes): the side-question fork. */
  "welcome-rose"?: string;
  /** Signed-out welcome page (light in both themes): the sent mark. */
  "welcome-ok"?: string;
  /** Signed-out welcome page (light in both themes): the ask icon. */
  "welcome-amber"?: string;
  /** Signed-out welcome page (light in both themes): the ask icon glyph. */
  "welcome-amber-ink"?: string;
  /** Signed-out welcome page (light in both themes): Superchat's machine window. */
  "welcome-pc"?: string;
  /** Signed-out welcome page (light in both themes): its text. */
  "welcome-pc-ink"?: string;
  /** Signed-out welcome page (light in both themes): its muted text. */
  "welcome-pc-mute"?: string;
  /** Signed-out welcome page (light in both themes): its inner page. */
  "welcome-pc-win"?: string;
  /** Signed-out welcome page (light in both themes): its inner page text. */
  "welcome-pc-win-ink"?: string;
  /** Signed-out welcome page (light in both themes): its inner page muted text. */
  "welcome-pc-win-mute"?: string;
  /** Signed-out welcome page (light in both themes): its chart bars. */
  "welcome-pc-bar"?: string;
  /** Signed-out welcome page (light in both themes): its live dot. */
  "welcome-live"?: string;
  /** Signed-out welcome page (light in both themes): a chip. */
  "welcome-chip"?: string;
  /** Signed-out welcome page (light in both themes): a chip border. */
  "welcome-chip-line"?: string;
  /** Signed-out welcome page (light in both themes): a chip text. */
  "welcome-chip-ink"?: string;
  /** Signed-out welcome page (light in both themes): the orb highlight. */
  "welcome-orb-hi"?: string;
  /** Signed-out welcome page (light in both themes): the orb body. */
  "welcome-orb-mid"?: string;
  /** Signed-out welcome page (light in both themes): the orb shade. */
  "welcome-orb-deep"?: string;
  /** Signed-out welcome page (light in both themes): the orb edge. */
  "welcome-orb-lo"?: string;
  /** Signed-out welcome page (light in both themes): the web tool glyph. */
  "welcome-violet"?: string;
  /** Signed-out welcome page (light in both themes): the artifact canvas. */
  "welcome-canvas"?: string;
  /** Signed-out welcome page (light in both themes): the side-chat list. */
  "welcome-panel"?: string;
  /** Signed-out welcome page (light in both themes): a switch that is off. */
  /** Signed-out welcome page (light in both themes): the recording label. */
  "welcome-rec"?: string;
  /** Signed-out welcome page (light in both themes): the recording dot. */
  "welcome-rec-dot"?: string;
  /** Signed-out welcome page (same in both themes): the night page. */
  "welcome-night"?: string;
  /** Signed-out welcome page (same in both themes): text on night. */
  "welcome-night-ink"?: string;
  /** Signed-out welcome page (same in both themes): secondary text on night. */
  "welcome-night-ink-2"?: string;
  /** Signed-out welcome page (same in both themes): tertiary text on night. */
  "welcome-night-ink-3"?: string;
  /** Signed-out welcome page (same in both themes): the hero sub line. */
  "welcome-night-sub"?: string;
  /** Signed-out welcome page (same in both themes): a demo card's body copy. */
  "welcome-night-body"?: string;
  /** Signed-out welcome page (same in both themes): the accent glow. */
  "welcome-night-glow"?: string;
  /** Signed-out welcome page (same in both themes): an eyebrow on a demo card. */
  "welcome-night-glow-ink"?: string;
  /** Signed-out welcome page (same in both themes): a chip's text on a demo card. */
  "welcome-night-glow-ink-2"?: string;
  /** Signed-out welcome page (same in both themes): the mark orb highlight. */
  "welcome-night-orb-hi"?: string;
  /** Signed-out welcome page (same in both themes): the mark orb shade. */
  "welcome-night-orb-lo"?: string;
  /** Signed-out welcome page (same in both themes): the fork and the cursor. */
  "welcome-night-rose"?: string;
  /** Signed-out welcome page (same in both themes): the ask icon on night. */
  "welcome-night-amber"?: string;
  /** Signed-out welcome page (same in both themes): Superchat's bubble on a demo card. */
  "welcome-night-bubble"?: string;
  /** Signed-out welcome page (same in both themes): a panel on a demo card. */
  "welcome-night-panel"?: string;
  /** Signed-out welcome page (same in both themes): a quiet button on a demo card. */
  "welcome-night-key"?: string;
  /** Signed-out welcome page (same in both themes): a switch that is off. */
  "welcome-night-switch"?: string;
  /** Signed-out welcome page (same in both themes): the film's Sound and Close pills. */
  "welcome-night-pill"?: string;
  /** Signed-out welcome page (same in both themes): the machine's status bar. */
  "welcome-night-bar"?: string;
  /** Signed-out welcome page (same in both themes): the recording label on night. */
  "welcome-night-rec-ink"?: string;
  /** Signed-out welcome page (same in both themes): a star in the field. */
  "welcome-night-star"?: string;
  /** Signed-out welcome page (same in both themes): a hairline on night. */
  "welcome-night-line"?: string;
  /** Signed-out welcome page (same in both themes): the machine's desktop, lit corner. */
  "welcome-screen-hi"?: string;
  /** Signed-out welcome page (same in both themes): the machine's desktop, middle. */
  "welcome-screen-mid"?: string;
  /** Signed-out welcome page (same in both themes): the machine's desktop, far corner. */
  "welcome-screen-lo"?: string;
  /** Signed-out welcome page (same in both themes): the light sheet under the night. */
  "welcome-sheet"?: string;
  /** Signed-out welcome page (same in both themes): a hairline on the sheet. */
  "welcome-sheet-line"?: string;
  /** Signed-out welcome page (same in both themes): a button border on the sheet. */
  "welcome-sheet-line-2"?: string;
  /** Signed-out welcome page (same in both themes): secondary text on the sheet. */
  "welcome-sheet-ink-2"?: string;
  /** Signed-out welcome page (same in both themes): muted text on a light surface. */
  "welcome-mute"?: string;
  /** Signed-out welcome page (same in both themes): the created document. */
  "welcome-doc"?: string;
  /** Signed-out welcome page (same in both themes): the created document's text. */
  "welcome-doc-ink"?: string;
  /** Signed-out welcome page (same in both themes): a window dot in the machine's browser. */
  "welcome-app-dot"?: string;
  /** Signed-out welcome page (same in both themes): an input border in the machine's browser. */
  "welcome-app-line"?: string;
  /** Signed-out welcome page (same in both themes): a row border in the machine's browser. */
  "welcome-app-line-2"?: string;
  /** Signed-out welcome page (same in both themes): a placeholder in the machine's browser. */
  "welcome-app-hint"?: string;
};

export const darkTokens = {
  background: "#0B0C0E",
  foreground: "#ECECEE",
  card: "#141518",
  cardForeground: "#ECECEE",
  popover: "#141518",
  popoverForeground: "#ECECEE",
  primary: "#F1F1EF",
  primaryForeground: "#0B0C0E",
  secondary: "#18191E",
  secondaryForeground: "#ECECEE",
  chatUser: "#22242B",
  chatUserForeground: "#ECECEE",
  muted: "#141518",
  mutedForeground: "#85858A",
  accent: "#1A1B20",
  accentForeground: "#ECECEE",
  destructive: "#EF4444",
  destructiveForeground: "#FFFFFF",
  border: "#1E2026",
  input: "#18191E",
  ring: "#3B82F6",
  sidebar: "#111215",
  sidebarForeground: "#ECECEE",
  sidebarBorder: "#1C1D22",
  sidebarAccent: "#1A1B20",
  sidebarAccentForeground: "#ECECEE",
  link: "#3B82F6",
  success: "#4ECB71",
  warning: "#E9C46A",
  overlay: "rgba(4, 4, 5, 0.72)",
  scrollbar: "#1E2026",
  scrollbarHover: "#2E313A",
} as const satisfies ColorTokens;

export const lightTokens = {
  background: "#FAFAF8",
  foreground: "#1A1A1A",
  card: "#FFFFFF",
  cardForeground: "#1A1A1A",
  popover: "#FFFFFF",
  popoverForeground: "#1A1A1A",
  primary: "#1A1A1A",
  primaryForeground: "#F1F1EF",
  secondary: "#F0F0ED",
  secondaryForeground: "#1A1A1A",
  chatUser: "#E2E2DC",
  chatUserForeground: "#1A1A1A",
  muted: "#F0F0ED",
  mutedForeground: "#6C6C70",
  accent: "#EAEAE6",
  accentForeground: "#1A1A1A",
  destructive: "#DC2626",
  destructiveForeground: "#FFFFFF",
  border: "#F0F0ED",
  input: "#EAEAE6",
  ring: "#6C6C70",
  sidebar: "#ECECE9",
  sidebarForeground: "#1A1A1A",
  sidebarBorder: "#E8E8E4",
  sidebarAccent: "#FFFFFF",
  sidebarAccentForeground: "#1A1A1A",
  link: "#2563EB",
  success: "#228B3B",
  warning: "#B7791F",
  overlay: "rgba(20, 20, 22, 0.45)",
  scrollbar: "#C8C8C4",
  scrollbarHover: "#A8A8A4",
} as const satisfies ColorTokens;

/**
 * Superchat's surfaces (the old design notes): a Mac app. The content sits in one rounded
 * `content` window on the `window` ground; the sidebar and inspector float inside it as
 * blurred `glass` panels with a half-pixel `glass-line`; iOS inset grouped lists sit on
 * `group`. Ink comes in three strengths, hairlines are `separator`. The Muse palettes below
 * map the shadcn slots onto these same values, so each color is written once. `background`
 * is the solid surface (shadcn controls paint it inside panels).
 *
 * Owner-approved exceptions to the monochrome rule (Apple Mac-app redesign): the `tint`
 * accent (the one primary action, the selected row, the person's bubble), the colored app
 * `tile-*` gradients, one signature color per section (`sig-*`), the red `alert` badge, the
 * dark `media-*` result tile.
 *
 * `ground`, `panel`, `solid`, `line`, `bubble` and `selection` are the older names the
 * screens still read; they now resolve to the window, glass, group, separator and fill.
 */
const tiles = {
  "tile-blue-from": "#3F9BFF",
  "tile-blue-to": "#0A7AFF",
  "tile-green-from": "#45D36C",
  "tile-green-to": "#2FB34F",
  "tile-yellow-from": "#FFD23F",
  "tile-yellow-to": "#FBB72C",
  "tile-orange-from": "#FFA63D",
  "tile-orange-to": "#FF8A00",
  "tile-purple-from": "#C977F0",
  "tile-purple-to": "#A347D6",
  "tile-red-from": "#FF6961",
  "tile-red-to": "#FF3B30",
  "tile-indigo-from": "#7D7AFF",
  "tile-indigo-to": "#5856D6",
  "tile-teal-from": "#5BD2E6",
  "tile-teal-to": "#30B0C7",
  "tile-gray-from": "#A1A1A6",
  "tile-gray-to": "#8E8E93",
  // The result tile (Conversation): a dark card with a blue glow, the same in both themes.
  media: "#000000",
  "media-deep": "#071A3D",
  "media-glow": "#0B3D91",
  "media-bar": "#2C2C2E",
  "media-bar-2": "#1C1C1E",
  "media-key-from": "#7FD4FF",
  "media-key-to": "#0060DF",
  // The Feed's other media tiles: warm, pink and green glows.
  "media-warm-glow": "#B25B00",
  "media-warm-deep": "#4A2300",
  "media-pink-glow": "#C21F54",
  "media-pink-deep": "#4B0B22",
  "media-green-glow": "#1F8F4A",
  "media-green-deep": "#0B3A1F",
  // The signed-out welcome page is intentionally light in both themes.
  "welcome-bg": "#F6F6F8",
  "welcome-paper": "#FFFFFF",
  "welcome-ink": "#111118",
  "welcome-ink-2": "#55556A",
  "welcome-ink-3": "#8A8A9C",
  "welcome-line": "#E4E4EC",
  "welcome-me": "#3F6DFF",
  "welcome-bubble": "#ECECF2",
  "welcome-rose": "#E2558F",
  "welcome-ok": "#1F9D63",
  "welcome-amber": "#F2A33A",
  "welcome-amber-ink": "#1A1200",
  "welcome-pc": "#0F1020",
  "welcome-pc-ink": "#ECEBF6",
  "welcome-pc-mute": "#B9B8CC",
  "welcome-pc-win": "#F3F3F8",
  "welcome-pc-win-ink": "#1A1A26",
  "welcome-pc-win-mute": "#8A8A98",
  "welcome-pc-bar": "#C9D0FF",
  "welcome-live": "#57D39A",
  "welcome-chip": "#F2F3FF",
  "welcome-chip-line": "#D9DCFF",
  "welcome-chip-ink": "#3B3F8F",
  "welcome-orb-hi": "#C9D2FF",
  "welcome-orb-mid": "#6B7CFF",
  "welcome-orb-deep": "#4B2FB0",
  "welcome-orb-lo": "#141433",
  "welcome-violet": "#7A5AF5",
  "welcome-canvas": "#FAFAFC",
  "welcome-panel": "#F4F4F7",
  "welcome-rec": "#C2335E",
  "welcome-rec-dot": "#FF4D6D",
  // The night hero, the statement and the demo cards (dark in both themes), and the sheet.
  "welcome-night": "#07070C",
  "welcome-night-ink": "#EEEDF7",
  "welcome-night-ink-2": "#A6A5BB",
  "welcome-night-ink-3": "#8584A0",
  "welcome-night-sub": "#C9C8DC",
  "welcome-night-body": "#C4C3D6",
  "welcome-night-glow": "#8B93FF",
  "welcome-night-glow-ink": "#C9CCFF",
  "welcome-night-glow-ink-2": "#D6D8FF",
  "welcome-night-orb-hi": "#C9CEFF",
  "welcome-night-orb-lo": "#3B2A8F",
  "welcome-night-rose": "#FF7AB6",
  "welcome-night-amber": "#FFB547",
  "welcome-night-bubble": "#1E1F33",
  "welcome-night-panel": "#17182B",
  "welcome-night-key": "#23243A",
  "welcome-night-switch": "#3A3B55",
  "welcome-night-pill": "#14141E",
  "welcome-night-bar": "#0A0A14",
  "welcome-night-rec-ink": "#FFB3C7",
  "welcome-night-star": "#DCE0FF",
  "welcome-night-line": "#D6D6FF",
  "welcome-screen-hi": "#3A2F86",
  "welcome-screen-mid": "#141634",
  "welcome-screen-lo": "#0A0B16",
  "welcome-sheet": "#F4F4F7",
  "welcome-sheet-line": "#DCDCE6",
  "welcome-sheet-line-2": "#C9C9D6",
  "welcome-sheet-ink-2": "#333333",
  "welcome-mute": "#6A6A7A",
  "welcome-doc": "#F5F4FA",
  "welcome-doc-ink": "#1B1B28",
  "welcome-app-dot": "#C4C4D0",
  "welcome-app-line": "#D6D6E2",
  "welcome-app-line-2": "#E6E6EE",
  "welcome-app-hint": "#9A9AA8",
} as const;

const superchatLight = {
  window: "#F5F5F7",
  content: "#FFFFFF",
  glass: "rgba(246, 246, 248, 0.78)",
  "glass-line": "rgba(0, 0, 0, 0.08)",
  group: "#FFFFFF",
  separator: "rgba(60, 60, 67, 0.12)",
  "selection-strong": "rgba(0, 113, 227, 0.12)",
  tint: "#0071E3",
  "tint-ink": "#0066CC",
  "tint-2": "#5AC8FA",
  "tint-3": "#7D5CFF",
  ok: "#34C759",
  warn: "#FF9F0A",
  alert: "#FF3B30",
  "sig-goals": "#34C759",
  "sig-feed": "#FF2D55",
  "sig-ideas": "#FF9F0A",
  "sig-library": "#5856D6",
  "sig-waiting": "#FF9500",
  ground: "#F5F5F7",
  "ground-wash": "#F5F5F7",
  panel: "rgba(246, 246, 248, 0.78)",
  solid: "#FFFFFF",
  line: "rgba(60, 60, 67, 0.12)",
  ink: "#1D1D1F",
  "ink-2": "#6E6E73",
  "ink-3": "#8E8E93",
  bubble: "#F2F2F7",
  selection: "rgba(0, 0, 0, 0.06)",
  ...tiles,
} as const;

const superchatDark = {
  window: "#1d242c",
  content: "#222a33",
  glass: "rgba(255, 255, 255, 0.07)",
  "glass-line": "rgba(255, 255, 255, 0.13)",
  group: "#2a323b",
  separator: "rgba(255, 255, 255, 0.12)",
  "selection-strong": "rgba(41, 151, 255, 0.18)",
  tint: "#0A84FF",
  "tint-ink": "#9FDCF5",
  "tint-2": "#64D2FF",
  "tint-3": "#9D8CFF",
  ok: "#30D158",
  warn: "#FF9F0A",
  alert: "#FF453A",
  "sig-goals": "#30D158",
  "sig-feed": "#FF375F",
  "sig-ideas": "#FF9F0A",
  "sig-library": "#5E5CE6",
  "sig-waiting": "#FF9F0A",
  ground: "#1d242c",
  "ground-wash": "#1d242c",
  panel: "rgba(255, 255, 255, 0.07)",
  solid: "#252c35",
  line: "rgba(255, 255, 255, 0.13)",
  ink: "#eef1f4",
  "ink-2": "#a9b3bd",
  "ink-3": "#9aa4ae",
  bubble: "#323c47",
  selection: "rgba(255, 255, 255, 0.08)",
  ...tiles,
} as const;

/**
 * Muse edition palettes. Light-first and quiet: ink primary, glass panels, hairline
 * borders. Applied only under `[data-product="muse"]`, so upstream Superchat keeps its own
 * palettes.
 */
export const museLightTokens = {
  background: superchatLight.solid,
  foreground: superchatLight.ink,
  card: superchatLight.solid,
  cardForeground: superchatLight.ink,
  popover: superchatLight.solid,
  popoverForeground: superchatLight.ink,
  primary: superchatLight.ink,
  primaryForeground: superchatLight.solid,
  secondary: superchatLight.selection,
  secondaryForeground: superchatLight.ink,
  chatUser: superchatLight.bubble,
  chatUserForeground: superchatLight.ink,
  muted: superchatLight.bubble,
  mutedForeground: superchatLight["ink-2"],
  accent: superchatLight.selection,
  accentForeground: superchatLight.ink,
  destructive: "#FF3B30",
  destructiveForeground: "#FFFFFF",
  border: superchatLight.line,
  input: superchatLight.line,
  // Keyboard focus: the accent at 50%.
  ring: "rgba(0, 113, 227, 0.5)",
  sidebar: superchatLight.glass,
  sidebarForeground: superchatLight.ink,
  sidebarBorder: superchatLight.line,
  sidebarAccent: superchatLight.selection,
  sidebarAccentForeground: superchatLight.ink,
  link: "#0066CC",
  success: "#248A3D",
  warning: "#C93400",
  overlay: "rgba(17, 18, 22, 0.32)",
  scrollbar: "#D6D8DC",
  scrollbarHover: "#B6B9BF",
  ...superchatLight,
} as const satisfies ColorTokens;

export const museDarkTokens = {
  background: superchatDark.solid,
  foreground: superchatDark.ink,
  card: superchatDark.solid,
  cardForeground: superchatDark.ink,
  popover: superchatDark.solid,
  popoverForeground: superchatDark.ink,
  primary: superchatDark.ink,
  primaryForeground: superchatDark.solid,
  secondary: superchatDark.selection,
  secondaryForeground: superchatDark.ink,
  chatUser: superchatDark.bubble,
  chatUserForeground: superchatDark.ink,
  muted: superchatDark.bubble,
  mutedForeground: superchatDark["ink-2"],
  accent: superchatDark.selection,
  accentForeground: superchatDark.ink,
  destructive: "#FF453A",
  destructiveForeground: "#FFFFFF",
  border: superchatDark.line,
  input: superchatDark.line,
  ring: "rgba(10, 132, 255, 0.55)",
  sidebar: superchatDark.glass,
  sidebarForeground: superchatDark.ink,
  sidebarBorder: superchatDark.line,
  sidebarAccent: superchatDark.selection,
  sidebarAccentForeground: superchatDark.ink,
  link: "#2997FF",
  success: "#30D158",
  warning: "#FF9F0A",
  overlay: "rgba(0, 0, 0, 0.6)",
  scrollbar: "#2F3036",
  scrollbarHover: "#43454C",
  ...superchatDark,
} as const satisfies ColorTokens;

/** Dark palette. Prefer `tokensForAppearance` when theme-aware. */
export const tokens = darkTokens;

export const RADIUS = "0.75rem";

export const botColors = [
  "#3EC5A8",
  "#F5A03C",
  "#6A6BF5",
  "#9B5CF6",
  "#3B82F6",
  "#F2622A",
  "#D9508A",
] as const;

export function isAppearancePreference(
  value: string | null | undefined,
): value is AppearancePreference {
  return value === "system" || value === "light" || value === "dark";
}

export function normalizeAppearancePreference(
  raw: string | null | undefined,
): AppearancePreference {
  return isAppearancePreference(raw) ? raw : "system";
}

export type ResolveAppearancePreferenceOptions = {
  stored?: string | null;
  storage?: Pick<Storage, "getItem"> | null;
};

function getLocalStorage(): Pick<Storage, "getItem" | "setItem"> | null {
  try {
    return typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    return null;
  }
}

export function resolveAppearancePreference(
  options: ResolveAppearancePreferenceOptions = {},
): AppearancePreference {
  const stored =
    options.stored !== undefined
      ? options.stored
      : readStoredAppearance(options.storage ?? getLocalStorage());
  return normalizeAppearancePreference(stored);
}

export function persistAppearancePreference(
  preference: AppearancePreference,
  storage: Pick<Storage, "setItem"> | null = getLocalStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(UI_APPEARANCE_STORAGE_KEY, preference);
  } catch {
    // Ignore quota / private-mode failures; in-memory preference still applies.
  }
}

export function resolveAppearance(
  preference: AppearancePreference,
  system: ResolvedAppearance = "dark",
): ResolvedAppearance {
  if (preference === "system") return system;
  return preference;
}

export function tokensForAppearance(appearance: ResolvedAppearance): ColorTokens {
  return appearance === "light" ? lightTokens : darkTokens;
}

function readStoredAppearance(storage: Pick<Storage, "getItem"> | null | undefined): string | null {
  if (!storage) return null;
  try {
    return storage.getItem(UI_APPEARANCE_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** `cardForeground` -> `--card-foreground` */
export function cssVariableName(token: keyof ColorTokens): string {
  return `--${token.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`;
}

function renderBlock(selector: string, colorScheme: ResolvedAppearance, palette: ColorTokens) {
  const lines = (Object.keys(palette) as (keyof ColorTokens)[])
    .filter((token) => palette[token] !== undefined)
    .map((token) => `  ${cssVariableName(token)}: ${(palette[token] as string).toLowerCase()};`);
  return `${selector} {\n  color-scheme: ${colorScheme};\n${lines.join("\n")}\n  --radius: ${RADIUS};\n}`;
}

/** The CSS in `tokens.css`. Generated from the TS palette so both stay in sync. */
export function renderTokensCss(): string {
  return `${[
    "/* Generated by `pnpm --filter @superchat/ui-tokens generate`. Edit src/index.ts instead. */",
    "/* Muse: tint, tile-*, sig-*, alert and media-* are owner-approved exceptions to the monochrome rule (Apple Mac-app design). */",
    renderBlock(':root,\n[data-theme="dark"]', "dark", darkTokens),
    renderBlock('[data-theme="light"]', "light", lightTokens),
    renderBlock('[data-product="muse"]', "light", museLightTokens),
    renderBlock('[data-product="muse"][data-theme="dark"]', "dark", museDarkTokens),
  ].join("\n\n")}\n`;
}
