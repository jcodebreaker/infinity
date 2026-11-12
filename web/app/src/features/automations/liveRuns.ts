import type { ArtifactVersion, AutomationRun, LivePage } from "@superchat/contracts";
import { describeSchedule } from "./schedule";

/** A version made by an update run lands within this long after the run finished (seconds). */
const SLACK_SECONDS = 120;
/** A run with no end yet (or one the engine lost) is given this long to make its version. */
const OPEN_RUN_SECONDS = 30 * 60;

const seconds = (iso: string | null) => (iso ? Date.parse(iso) / 1000 : null);

/** The update version each run made: the one that records the run's chat. A version saved before
 * the engine kept that is matched by time: the run that fired last before the version was saved,
 * if the version came before that run ended. */
export function versionsByRun(
  runs: readonly AutomationRun[],
  versions: readonly ArtifactVersion[],
): Map<string, ArtifactVersion> {
  const made = new Map<string, ArtifactVersion>();
  for (const version of versions) {
    if (version.origin !== "update") continue;
    if (version.chatId) {
      const run = runs.find((candidate) => candidate.chatId === version.chatId);
      if (run && !made.has(run.id)) made.set(run.id, version);
      continue;
    }
    const at = seconds(version.createdAt);
    if (at === null) continue;
    let best: { run: AutomationRun; fired: number } | null = null;
    for (const run of runs) {
      const fired = seconds(run.firedAt);
      if (fired === null || fired > at + 1) continue;
      if (!best || fired > best.fired) best = { run, fired };
    }
    if (!best) continue;
    const end = seconds(best.run.finishedAt) ?? best.fired + OPEN_RUN_SECONDS;
    if (at <= end + SLACK_SECONDS && !made.has(best.run.id)) made.set(best.run.id, version);
  }
  return made;
}

/** The engine's failure code as a short phrase ("run_timeout" -> "run timeout"). */
export const plainCode = (code: string | null): string | null =>
  code ? code.replace(/[_-]+/g, " ").toLowerCase() : null;

/** "Mon 8:00" for an engine timestamp. */
export function whenShort(iso: string | null | undefined, locale?: string): string {
  const date = iso ? new Date(iso) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(locale || undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** "Mon 8:00" for epoch seconds. */
export const whenSeconds = (epochSeconds: number, locale?: string) =>
  whenShort(new Date(epochSeconds * 1000).toISOString(), locale);

/** The file names a page's updates follow: "report.csv", "2 files", or none (web pages only). */
export function watchedFiles(page: LivePage): string[] {
  if (!page.onChange) return [];
  return page.sources
    .filter((source) => !/^[a-z][a-z0-9+.-]*:\/\//i.test(source))
    .map((source) => source.split("/").filter(Boolean).pop() ?? source);
}

/** "When report.csv changes · Every Monday at 08:00", or only the schedule. */
export function liveTrigger(page: LivePage, words: { when: (files: string) => string }): string {
  const files = watchedFiles(page);
  const schedule = describeSchedule(page.schedule);
  if (!files.length) return schedule;
  const named = files.length === 1 ? (files[0] ?? "") : `${files.length} files`;
  return `${words.when(named)} · ${schedule}`;
}

export type LiveStatus =
  | { kind: "paused"; failures: number }
  | { kind: "failed"; reason: string }
  | { kind: "changed"; changes: number; at: number }
  | { kind: "checked"; at: number }
  | { kind: "never" };

/** The last run of a live page, read from its state: paused, failed, changed or checked. */
export function liveStatus(page: LivePage): LiveStatus {
  if (page.paused) return { kind: "paused", failures: page.failures };
  if (page.failures > 0 && page.lastFailure) {
    return { kind: "failed", reason: page.lastFailure.reason };
  }
  const update = page.lastUpdate;
  // A check always follows the update it made; one clearly later found nothing new.
  if (update?.at && update.changes > 0 && (!page.checkedAt || page.checkedAt - update.at < 120)) {
    return { kind: "changed", changes: update.changes, at: update.at };
  }
  if (page.checkedAt) return { kind: "checked", at: page.checkedAt };
  return { kind: "never" };
}
