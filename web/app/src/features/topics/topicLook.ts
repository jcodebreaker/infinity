import type { Topic, TopicOpen } from "@superchat/contracts";
import { daysAgo } from "./topicTime";

// How a topic looks on Home (ADR 0035, amended "Home tiles, status and tags"): its two hues, its
// tag, its status and which topic leads the row. Pure: free of React and i18n; the views word it.

/** FNV-1a over the key: the same key always gives the same number, on every client. */
function hash(key: string): number {
  let h = 0x811c9dc5;
  for (let index = 0; index < key.length; index += 1) {
    h ^= key.charCodeAt(index);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** A topic's two hues (degrees): the wash's main colour and its neighbour. */
export type TopicHues = { h1: number; h2: number };

/** The curated pairs a topic's wash is drawn from, well apart around the wheel so neighbouring
 * tiles never read as one colour: coral, warm sand, lemon, mint and teal, ocean, sky and lilac,
 * lavender, rose and peach. Lightness and saturation are the theme's. */
export const TOPIC_PAIRS: readonly TopicHues[] = [
  { h1: 8, h2: 28 },
  { h1: 36, h2: 20 },
  { h1: 54, h2: 78 },
  { h1: 150, h2: 172 },
  { h1: 188, h2: 210 },
  { h1: 214, h2: 255 },
  { h1: 266, h2: 290 },
  { h1: 332, h2: 16 },
];

/** Each key's pair: its own by hash, or the next free one when an earlier key (in key order,
 * so the result does not depend on which topics are listed first) already holds it. Once every
 * pair is taken, keys share again. */
export function topicPalette(keys: readonly string[]): Map<string, TopicHues> {
  const out = new Map<string, TopicHues>();
  const taken = new Set<number>();
  for (const key of [...new Set(keys)].sort()) {
    let index = hash(key) % TOPIC_PAIRS.length;
    if (taken.size < TOPIC_PAIRS.length) {
      while (taken.has(index)) index = (index + 1) % TOPIC_PAIRS.length;
    }
    taken.add(index);
    out.set(key, TOPIC_PAIRS[index] ?? { h1: 214, h2: 255 });
  }
  return out;
}

/** The key's pair from *palette*, else its own by hash. */
export function topicHues(key: string, palette?: ReadonlyMap<string, TopicHues>): TopicHues {
  return palette?.get(key) ?? TOPIC_PAIRS[hash(key) % TOPIC_PAIRS.length] ?? { h1: 214, h2: 255 };
}

/** Words that never name a topic on their own. */
const PLAIN_WORDS = new Set([
  "a",
  "an",
  "the",
  "my",
  "our",
  "your",
  "new",
  "to",
  "for",
  "of",
  "and",
  "on",
  "in",
  "with",
  "about",
  "draft",
  "review",
  "update",
  "weekly",
  "monthly",
]);

/** The topic's tag: what the writer named it, else the first word of its title that names
 * something (a short one takes its neighbour: "Q3 plan", "EU AI Act" → "EU AI"). */
export function topicTag(topic: Pick<Topic, "tag" | "title">): string {
  const given = topic.tag?.trim();
  if (given) return given;
  const words = topic.title.trim().split(/\s+/).filter(Boolean);
  const start = words.findIndex((word) => !PLAIN_WORDS.has(word.toLowerCase()));
  if (start < 0) return words.slice(0, 2).join(" ");
  const first = words[start] ?? "";
  const next = words[start + 1];
  return first.length <= 3 && next ? `${first} ${next}` : first;
}

/** An open line with a real due day, and how many days until it (negative: past). */
type Dated = { line: TopicOpen; due: string; ahead: number };

/** The topic's open lines that carry a date, soonest first. */
function datedLines(topic: { open: readonly TopicOpen[] }, now: Date): Dated[] {
  const out: Dated[] = [];
  for (const line of topic.open) {
    if (!line.due) continue;
    const at = new Date(`${line.due}T12:00:00`).getTime() / 1000;
    if (Number.isNaN(at)) continue;
    out.push({ line, due: line.due, ahead: -daysAgo(at, now) });
  }
  return out.sort((a, b) => a.ahead - b.ahead);
}

/** What a tile says under its title, before wording:
 * - `line`: the writer's (or the Lookout's) own status line: where the work stands, with the
 *   due day of a promise due today or past as a hint;
 * - `open`: the next open line, with its due day when it has one;
 * - `done`: nothing is open. */
export type TopicStatus =
  | { kind: "line"; text: string; due: string | null; needsYou: boolean }
  | { kind: "open"; text: string; due: string | null; needsYou: true }
  | { kind: "done"; needsYou: false };

export function topicStatus(topic: Pick<Topic, "statusLine" | "open">, now: Date): TopicStatus {
  const needsYou = topic.open.length > 0;
  const line = topic.statusLine?.trim();
  const dated = datedLines(topic, now);
  if (line) {
    const due = dated[0] && dated[0].ahead <= 0 ? dated[0].due : null;
    return { kind: "line", text: line, due, needsYou };
  }
  const next = dated[0]?.line ?? topic.open[0];
  if (next) return { kind: "open", text: next.text, due: next.due ?? null, needsYou: true };
  return { kind: "done", needsYou: false };
}

/** Whether the person worked on *topic* (picked it up, or the Conversation wrote it) after *at*;
 * with no *at*, at any time today. */
function workedSince(topic: Pick<Topic, "workedAt">, now: Date, at?: number): boolean {
  const worked = topic.workedAt ?? 0;
  if (!worked) return false;
  return at === undefined ? daysAgo(worked, now) <= 0 : worked >= at;
}

/** The topic that leads Home, and why:
 * - `prepared`: the Lookout prepared a draft the person has not opened (newest first);
 * - `due`: else the topic whose dated promise is due soonest, today or already past.
 * A topic the person already worked on for that reason (after the draft came, or today for a
 * due promise) does not lead for it again; the next candidate does.
 * `null` when none: every tile is then the same size. */
export type FeatureTopic =
  | { topic: Topic; why: "prepared"; due: Dated | null }
  | { topic: Topic; why: "due"; due: Dated };

export function featureTopic(topics: readonly Topic[], now: Date): FeatureTopic | null {
  const dueBy = (topic: Topic) => datedLines(topic, now).find((dated) => dated.ahead <= 0) ?? null;
  const prepared = topics
    .filter(
      (topic) => topic.prepared?.artifactId && !workedSince(topic, now, topic.prepared.at ?? 0),
    )
    .sort((a, b) => (b.prepared?.at ?? 0) - (a.prepared?.at ?? 0))[0];
  if (prepared) return { topic: prepared, why: "prepared", due: dueBy(prepared) };
  let best: { topic: Topic; due: Dated } | null = null;
  for (const topic of topics) {
    if (workedSince(topic, now)) continue;
    const due = dueBy(topic);
    if (due && (!best || due.ahead < best.due.ahead)) best = { topic, due };
  }
  return best ? { ...best, why: "due" } : null;
}

/** A status worded from an open line: its text as written, then " · <due words>" unless the
 * whole would pass *limit* characters (then the text alone; the tile wraps to two lines). */
export function openStatus(text: string, due: string, limit = 70): string {
  const flat = text.replace(/\s+/g, " ").trim();
  const whole = due ? `${flat} · ${due}` : flat;
  return whole.length <= limit ? whole : flat;
}

/** The first sentences of *text* that fit in *limit* characters (always at least the first),
 * so a feature tile is short by what it says, never cut by CSS. */
export function firstSentences(text: string, limit = 170): string {
  const flat = text.replace(/\s+/g, " ").trim();
  const sentences = flat.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) ?? [flat];
  let out = "";
  for (const sentence of sentences) {
    if (out && (out + sentence).trim().length > limit) break;
    out += sentence;
  }
  return out.trim();
}

/** Words that never pair a starter with an open line on their own. */
const STOP_WORDS = new Set([
  ...PLAIN_WORDS,
  "i",
  "me",
  "we",
  "us",
  "you",
  "it",
  "its",
  "is",
  "be",
  "or",
  "at",
  "by",
  "as",
  "from",
  "this",
  "that",
  "these",
  "those",
  "them",
  "their",
  "whether",
  "what",
  "which",
  "can",
  "will",
  "still",
  "all",
  "any",
  "some",
  "up",
  "out",
]);

function contentWords(text: string): Set<string> {
  const words = text.toLowerCase().split(/[^\p{L}\p{N}]+/u);
  return new Set(words.filter((word) => word && !STOP_WORDS.has(word)));
}

/** A starter's first word as its button's word ("Draft my message…" -> "Draft"), for a row the
 * writer gave no `verb`; `null` (the view's "Start") when there is no starter. */
function leadWord(starter: string | undefined): string | null {
  const word = starter
    ?.trim()
    .split(/\s+/)[0]
    ?.replace(/[^\p{L}\p{N}'-]/gu, "");
  return word ? word.charAt(0).toLocaleUpperCase() + word.slice(1) : null;
}

/** One row of the pick-up card: an open line, or a starter no line carries (`kind` null), with
 * the request its button puts in the composer (`null`: the row's own text) and the button's
 * word (`null`: the view's "Start"). */
export type PickRow = {
  key: string;
  /** The open line's id, which the writer passes back to keep it. */
  id: string | null;
  kind: TopicOpen["kind"] | null;
  text: string;
  due: string | null;
  starter: string | null;
  verb: string | null;
};

/** The pick-up card's rows (ADR 0035, amended 2026-10-25): `next` is the open line due soonest
 * today or already past, else the first Promised, else the one due soonest, else the first;
 * `also` the other open lines in order, then the starters no line carries.
 *
 * A line without its own `starter` takes the loose starter it shares the most content words
 * with (at least two), each starter used once; a starter equal to a line's own is dropped. With
 * no open line, the first loose starter is `next`. */
export function pickRows(
  topic: { open: readonly TopicOpen[]; starters: readonly string[] },
  now: Date,
): { next: PickRow | null; also: PickRow[] } {
  const norm = (text: string) => text.replace(/\s+/g, " ").trim().toLowerCase();
  const own = new Set(topic.open.flatMap((line) => (line.starter ? [norm(line.starter)] : [])));
  const loose = topic.starters.filter((starter) => !own.has(norm(starter)));
  const paired = new Map<TopicOpen, string>();
  const pairs: { line: TopicOpen; starter: string; score: number }[] = [];
  for (const starter of loose) {
    const words = contentWords(starter);
    for (const line of topic.open) {
      if (line.starter) continue;
      let score = 0;
      for (const word of contentWords(line.text)) if (words.has(word)) score += 1;
      if (score >= 2) pairs.push({ line, starter, score });
    }
  }
  const used = new Set<string>();
  // Stable sort: on a tie, the earlier starter and then the earlier line pair first.
  for (const { line, starter } of pairs.sort((a, b) => b.score - a.score)) {
    if (paired.has(line) || used.has(starter)) continue;
    paired.set(line, starter);
    used.add(starter);
  }
  const rows = topic.open.map(
    (line): PickRow => ({
      key: line.id ?? `${line.kind}:${line.text}`,
      id: line.id ?? null,
      kind: line.kind,
      text: line.text,
      due: line.due ?? null,
      starter: line.starter ?? paired.get(line) ?? null,
      verb: line.starter ? (line.verb ?? leadWord(line.starter)) : leadWord(paired.get(line)),
    }),
  );
  const extra = loose
    .filter((starter) => !used.has(starter))
    .map(
      (starter): PickRow => ({
        key: `starter:${starter}`,
        id: null,
        kind: null,
        text: starter,
        due: null,
        starter,
        verb: leadWord(starter),
      }),
    );
  const dated = datedLines(topic, now);
  const lead =
    dated.find((entry) => entry.ahead <= 0)?.line ??
    topic.open.find((line) => line.kind === "Promised") ??
    dated[0]?.line ??
    topic.open[0];
  const index = lead ? topic.open.indexOf(lead) : -1;
  if (index < 0) return { next: extra[0] ?? null, also: extra.slice(1) };
  return {
    next: rows[index] ?? null,
    also: [...rows.filter((_, at) => at !== index), ...extra],
  };
}

/** The message "Work through all" sends: what the person reads in their bubble, one plain line
 * per row with its due day (the topic's key and ids reach the assistant with the pick). */
export function workAllText(ask: string, rows: readonly PickRow[]): string {
  const lines = rows.map((row) => {
    const meta = [row.kind === "Promised" ? "promised" : "", row.due ? `due ${row.due}` : ""]
      .filter(Boolean)
      .join(", ");
    return `- ${row.text}${meta ? ` (${meta})` : ""}`;
  });
  return [`${ask}:`, ...lines].join("\n");
}
