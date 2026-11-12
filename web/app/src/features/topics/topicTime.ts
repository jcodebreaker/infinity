import type { Topic } from "@superchat/contracts";

// Pure helpers for topics' times (ADR 0035): local days, the week strip, the "New" window, and
// the Topics panel's by-day grouping. Free of React and i18n; the views word the labels.

const HOUR = 3600;
const DAY_MS = 86_400_000;
/** A topic's "New" line shows this long after topic watch set it. */
export const FRESH_SECONDS = 48 * HOUR;
/** Home's default range, and its wider one. */
export const RANGE_HOURS = { recent: 48, week: 7 * 24 } as const;
export type TopicRange = keyof typeof RANGE_HOURS;

/** Local midnight of the day *at* (epoch seconds) falls on. */
function dayStart(atMs: number): number {
  const date = new Date(atMs);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

/** Whole local days between two instants (0 = the same day). */
export function daysAgo(at: number, now: Date): number {
  return Math.round((dayStart(now.getTime()) - dayStart(at * 1000)) / DAY_MS);
}

/** The local `YYYY-MM-DD` of *at* (epoch seconds). */
export function localDay(at: number): string {
  const date = new Date(at * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** Seven flags, oldest first ending today: whether the topic has a moment on that day. */
export function weekStrip(topic: Pick<Topic, "events" | "lastTouchedAt">, now: Date): boolean[] {
  const touched = new Set<number>();
  for (const event of topic.events) touched.add(daysAgo(event.at, now));
  touched.add(daysAgo(topic.lastTouchedAt, now));
  return Array.from({ length: 7 }, (_, index) => touched.has(6 - index));
}

/** Whether the topic was touched within *range*. */
export function inRange(topic: Pick<Topic, "lastTouchedAt">, range: TopicRange, now: Date) {
  return now.getTime() / 1000 - topic.lastTouchedAt <= RANGE_HOURS[range] * HOUR;
}

/** Whether topic watch's "New" line is set and still fresh. */
export function isFresh(topic: Pick<Topic, "since" | "sinceAt">, now: Date): boolean {
  return Boolean(
    topic.since && topic.sinceAt && now.getTime() / 1000 - topic.sinceAt <= FRESH_SECONDS,
  );
}

/** How high a topic's peak rises under its card (0 to 1): recent and busy topics stand taller. */
export function peakHeat(topic: Pick<Topic, "events" | "lastTouchedAt">, now: Date): number {
  const hours = Math.max(0, now.getTime() / 1000 - topic.lastTouchedAt) / HOUR;
  const recency = Math.max(0, 1 - hours / RANGE_HOURS.week);
  const busy = Math.min(1, topic.events.length / 8);
  return Math.round((0.25 + 0.55 * recency + 0.2 * busy) * 100) / 100;
}

/** One moment of a topic: a dated line, and whether it is topic watch's fresh "New" line. */
export type TopicMoment = { at: number; line: string; topic: Topic; fresh: boolean };

/** One row of the Topics panel: a topic and its moments on one day, newest first. */
export type TopicDayRow = { topic: Topic; moments: [TopicMoment, ...TopicMoment[]] };

/** Each local day, newest first, with one row per topic touched that day (ordered by its newest
 * moment); a topic can show on several days. */
export function topicsByDay(
  topics: readonly Topic[],
  now: Date,
): { day: string; ago: number; rows: TopicDayRow[] }[] {
  const moments: TopicMoment[] = [];
  for (const topic of topics) {
    for (const event of topic.events) moments.push({ ...event, topic, fresh: false });
    // A topic with no dated moment still has a day: when it was last touched, with where it stands.
    if (topic.events.length === 0 && topic.lastTouchedAt) {
      moments.push({ at: topic.lastTouchedAt, line: topic.leftOff, topic, fresh: false });
    }
    if (topic.since && topic.sinceAt && isFresh(topic, now)) {
      moments.push({ at: topic.sinceAt, line: topic.since, topic, fresh: true });
    }
  }
  moments.sort((a, b) => b.at - a.at);
  const days: { day: string; ago: number; rows: TopicDayRow[] }[] = [];
  for (const moment of moments) {
    const day = localDay(moment.at);
    let last = days.at(-1);
    if (last?.day !== day) {
      last = { day, ago: daysAgo(moment.at, now), rows: [] };
      days.push(last);
    }
    const row = last.rows.find((candidate) => candidate.topic.key === moment.topic.key);
    if (row) row.moments.push(moment);
    else last.rows.push({ topic: moment.topic, moments: [moment] });
  }
  return days;
}
