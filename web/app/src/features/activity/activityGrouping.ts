import type { Activity, ActivityStatus } from "@superchat/contracts";
import { markdownToLine } from "../../lib/markdown-plain-text";

// Pure helpers for the Activity panel (docs/helpers.md "The activity panel";
// docs/helpers.md "The activity panel"): grouping by day, the line shown under a title,
// clock times, and the polling cadence decision. Kept free of React/i18n so
// activityGrouping.test.ts can exercise every branch directly.

/** The feed re-reads on the engine's live signal (`activities.watch`); these are only the
 * safety net under it, and the whole cadence when the signal is unavailable. While anything is
 * running the net is tight, since a Helper's steps land continuously. */
export const ACTIVITY_POLL_ACTIVE_MS = 3_000;
export const ACTIVITY_POLL_IDLE_MS = 20_000;
export const ACTIVITY_POLL_IDLE_WATCHED_MS = 60_000;

/** True while this Activity is still being worked on. */
export function isRunning(activity: Pick<Activity, "status">): boolean {
  return activity.status === "in_progress";
}

/** The re-read cadence: tight while any Activity is running; otherwise relaxed, and more so
 * while the live signal is connected (it would have said so). */
export function activitiesPollIntervalMs(
  activities: readonly Activity[],
  watching = false,
): number {
  if (activities.some(isRunning)) return ACTIVITY_POLL_ACTIVE_MS;
  return watching ? ACTIVITY_POLL_IDLE_WATCHED_MS : ACTIVITY_POLL_IDLE_MS;
}

/** A Helper's whole task (docs/glossary.md "Helper"; engine "sub_agent"), whose run page can show
 * the Helper's own messages, as opposed to a plain Conversation/Side Chat turn ("turn"). A turn
 * in a file's studio is never one, whatever the engine stores it as (ADR 0024). */
export function isHelperActivity(activity: Pick<Activity, "kind" | "source">): boolean {
  return activity.kind === "sub_agent" && activity.source !== "studio";
}

/** One line of a day in the panel: an Activity, or a file's studio turns under its name. */
export type ActivityEntry =
  | { kind: "activity"; activity: Activity }
  | { kind: "file"; groupId: string; name: string; activities: Activity[] };

/** Studio turns (ADR 0024) gather under their file: one entry per file, where its newest turn
 * is, holding its turns in the order given. A studio turn that names no file stays a row. */
export function groupStudioTurns(activities: readonly Activity[]): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  const files = new Map<string, Extract<ActivityEntry, { kind: "file" }>>();
  for (const activity of activities) {
    const file = activity.source === "studio" ? activity.artifact : undefined;
    if (!file) {
      entries.push({ kind: "activity", activity });
      continue;
    }
    const entry = files.get(file.groupId);
    if (entry) {
      entry.activities.push(activity);
      continue;
    }
    const created = {
      kind: "file" as const,
      groupId: file.groupId,
      name: file.name,
      activities: [activity],
    };
    files.set(file.groupId, created);
    entries.push(created);
  }
  return entries;
}

/** The line shown under an Activity's title: its one-line summary (else its outcome) when
 * there is one, otherwise a plain status word for the ones that never get one — never "null"
 * or a blank line. While in progress the panel shows the live Step instead (presentStep). */
export function activityLineText(
  activity: Pick<Activity, "status" | "summary" | "outcome">,
  labels: Record<ActivityStatus, string>,
): string {
  const text = activity.summary ?? activity.outcome;
  return (text && markdownToLine(text)) || labels[activity.status];
}

/** A clock time such as "12:44 PM" in the viewer's own zone and locale — the day is
 * already the group's heading, so rows carry only the time. */
export function formatClockTime(iso: string, locale = "en"): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : new Intl.DateTimeFormat(locale, { timeStyle: "short" }).format(date);
}

/** Day and clock time ("Oct 3, 12:44 PM"), for the run page, which has no day heading. */
export function formatDayAndTime(iso: string, locale = "en"): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : new Intl.DateTimeFormat(locale, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }).format(date);
}

function dateKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export type ActivityDateLabel =
  | { kind: "today" }
  | { kind: "yesterday" }
  | { kind: "weekday"; text: string };

/** A group's heading: "Today", "Yesterday", or a weekday + date for anything older
 * (README.md "grouped by day"). `now` is passed in so this stays pure and testable
 * without mocking the clock. */
export function activityDateLabel(date: string, now: Date, locale = "en"): ActivityDateLabel {
  const today = dateKey(now);
  const yesterday = dateKey(new Date(now.getTime() - 24 * 60 * 60 * 1000));
  if (date === today) return { kind: "today" };
  if (date === yesterday) return { kind: "yesterday" };
  const parsed = new Date(`${date}T00:00:00`);
  const text = new Intl.DateTimeFormat(locale, {
    weekday: "short",
    month: "short",
    day: "numeric",
  }).format(parsed);
  return { kind: "weekday", text };
}

export interface ActivityGroup {
  date: string;
  label: ActivityDateLabel;
  activities: Activity[];
}

/** Groups Activities — already newest-first from `activities.list` — by calendar day,
 * keeping that order. The engine's own `date` field is in its own (UTC) terms, so this
 * ignores it and derives the day from `startedAt` in the viewer's local timezone instead
 * — the point of "grouped by day" is the day it looked like to the person, not to the
 * server. A day's Activities are always contiguous in a newest-first list, so this never
 * needs to re-sort, only to notice when the local day changes. */
export function groupActivitiesByDate(
  activities: readonly Activity[],
  now: Date,
  locale = "en",
): ActivityGroup[] {
  const groups: ActivityGroup[] = [];
  // A Side Chat that knows the conversation carries copies of its turns: the same turn
  // (same response id, the tail of the id) is listed once, from the first chat to have it.
  const seenTurns = new Set<string>();
  for (const activity of activities) {
    if (activity.kind === "turn") {
      const responseId = activity.id.slice(activity.id.lastIndexOf(":") + 1);
      if (seenTurns.has(responseId)) continue;
      seenTurns.add(responseId);
    }
    const localDate = dateKey(new Date(activity.startedAt));
    const last = groups.at(-1);
    if (last && last.date === localDate) {
      last.activities.push(activity);
      continue;
    }
    groups.push({
      date: localDate,
      label: activityDateLabel(localDate, now, locale),
      activities: [activity],
    });
  }
  return groups;
}

/**
 * Merges a freshly-polled newest page into the Activity list already loaded (which may
 * include older pages fetched via "Load earlier"): updates rows the newest page also
 * has (by id, in place — so an Activity that finished since the last poll doesn't jump
 * position), and prepends any genuinely new ones in front. Never drops a row the newest
 * page doesn't mention — that's how "Load earlier" survives the next poll.
 */
export function mergeNewestPage(
  current: readonly Activity[],
  newest: readonly Activity[],
): Activity[] {
  const freshById = new Map(newest.map((activity) => [activity.id, activity]));
  const updatedExisting = current.map((activity) => freshById.get(activity.id) ?? activity);
  const currentIds = new Set(current.map((activity) => activity.id));
  const newOnes = newest.filter((activity) => !currentIds.has(activity.id));
  return [...newOnes, ...updatedExisting];
}

/** A span as people say it: "12s", "1m 20s", "1h 5m". Whole seconds, never negative. */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** How long an Activity has been going (running) or took (settled), or null when unknown. */
export function activityDurationMs(
  activity: Pick<Activity, "status" | "startedAt" | "finishedAt">,
  nowMs: number,
): number | null {
  const start = Date.parse(activity.startedAt);
  const end = isRunning(activity) ? nowMs : Date.parse(activity.finishedAt ?? "");
  return Number.isNaN(start) || Number.isNaN(end) ? null : Math.max(0, end - start);
}
