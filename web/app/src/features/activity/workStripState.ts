import type { Activity, MessageBlock, ThreadMessage } from "@superchat/contracts";
import type { ActivityNode } from "./activityTree";
import { buildActivityForest, findHelperNode, isNodeRunning } from "./activityTree";
import { serverNow } from "./superchatWork";
import { ARTIFACT_SAVE_TOOL, FLAG_PREFIX, FLAG_TOOL, PROGRESS_TOOL } from "./toolNames";
import type { TurnOutcome } from "./workPlan";

// The work strip under a reply (docs/conversation.md "The assistant narrates while helpers
// work"): pure readings of the Activity feed for the Helpers a message started, so the strip,
// its tests and the dev preview share one interpretation. A Helper's `report_progress` and
// `raise_flag` calls are Steps of its Activity (engine `step_title_for_call`): the progress text
// as the title, a flag as "Flagged: <text>".

/** A Helper as the transcript names it: its chat id, a fallback title, its status then. */
export type StripHelper = Pick<
  Extract<MessageBlock, { kind: "helper" }>,
  "helperId" | "title" | "status"
>;

/** One running Helper's line. */
export interface WorkLine {
  helperId: string;
  title: string;
  /** Its latest progress sentence; null until it has reported one. */
  progress: string | null;
  /** What it flagged, oldest first. */
  flags: string[];
  /** How long it has been going; null before the feed lists it. */
  elapsedMs: number | null;
}

export type WorkStripState =
  | { kind: "working"; lines: WorkLine[]; focusId: string | null }
  | {
      kind: "settled";
      outcome: "done" | "failed" | "cancelled";
      /** First start to last finish; null when the feed has none of them. */
      ms: number | null;
      /** The last finish (ms); null when the feed has none of them. */
      endedAt: number | null;
      focusId: string | null;
    };

/** A Helper's latest progress sentence, if it has reported one. */
export function latestProgress(activity: Pick<Activity, "steps">): string | null {
  const steps = activity.steps ?? [];
  for (let i = steps.length - 1; i >= 0; i--) {
    const step = steps[i];
    if (step?.tool === PROGRESS_TOOL && step.title) return step.title;
  }
  return null;
}

/** What a Helper flagged, in its own words, oldest first. */
export function flagsOf(activity: Pick<Activity, "steps">): string[] {
  return (activity.steps ?? [])
    .filter((step) => step.tool === FLAG_TOOL && step.title)
    .map((step) =>
      step.title.startsWith(FLAG_PREFIX) ? step.title.slice(FLAG_PREFIX.length) : step.title,
    );
}

/** How many times these Activities saved a file: a new save is the cue to look for a draft. */
export function artifactSaveCount(activities: readonly Activity[]): number {
  return activities.reduce(
    (count, activity) =>
      count + (activity.steps ?? []).filter((step) => step.tool === ARTIFACT_SAVE_TOOL).length,
    0,
  );
}

/** A span on a clock: "0:41", "2:14", "1:02:09". Whole seconds, never negative. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
}

/** The Activities of these Helpers that the feed lists (their own rows, not their parts). */
export function helperActivities(
  helpers: readonly StripHelper[],
  activities: readonly Activity[] | undefined,
): Activity[] {
  if (!activities) return [];
  const forest = buildActivityForest(activities);
  return helpers
    .map((helper) => findHelperNode(forest, helper.helperId)?.activity)
    .filter((activity): activity is Activity => activity !== undefined);
}

/** When each Helper the loaded feed does not list was first found missing (client ms): held by
 * the view that asks (`useFirstMissing`). */
export type FirstMissing = ReadonlyMap<string, number>;
const NONE_MISSING: FirstMissing = new Map();

/**
 * The strip for the Helpers one message started. Working while any of them (or a part it handed
 * on) runs: one line per running Helper. Otherwise settled, with the whole span. `now` is the
 * client clock (`serverNow` turns it to the engine's).
 */
export function deriveWorkStrip(
  helpers: readonly StripHelper[],
  activities: readonly Activity[] | undefined,
  now: number,
  {
    turnRunning = false,
    firstMissing = NONE_MISSING,
    lit = true,
  }: {
    /** The Conversation's turn runs right now. */
    turnRunning?: boolean;
    firstMissing?: FirstMissing;
    /** Anything in the Conversation runs (the work state's `running`); when not, nothing here does. */
    lit?: boolean;
  } = {},
): WorkStripState {
  const forest = activities ? buildActivityForest(activities) : [];
  const read = helpers.map((helper) => {
    const node = findHelperNode(forest, helper.helperId);
    return {
      helper,
      activity: node?.activity,
      running:
        lit &&
        (node
          ? isNodeRunning(node)
          : unlistedRunning(helper, activities, turnRunning, now, firstMissing)),
    };
  });
  const running = read.filter((entry) => entry.running);
  const focusId = (running[0] ?? read.find((entry) => entry.activity))?.activity?.id ?? null;
  if (running.length > 0) {
    return {
      kind: "working",
      focusId,
      lines: running.map(({ helper, activity }) => ({
        helperId: helper.helperId,
        title: activity?.title || helper.title,
        progress: activity ? latestProgress(activity) : null,
        flags: activity ? flagsOf(activity) : [],
        elapsedMs: activity ? Math.max(0, serverNow(now) - Date.parse(activity.startedAt)) : null,
      })),
    };
  }
  const statuses = read.map((entry) => entry.activity?.status ?? entry.helper.status);
  const outcome = statuses.includes("failed")
    ? "failed"
    : statuses.length > 0 && statuses.every((status) => status === "cancelled")
      ? "cancelled"
      : "done";
  const listed = read.flatMap((entry) => (entry.activity ? [entry.activity] : []));
  const starts = listed.map((activity) => Date.parse(activity.startedAt));
  const ends = listed.map((activity) => Date.parse(activity.finishedAt ?? ""));
  const known = listed.length > 0 && [...starts, ...ends].every((value) => !Number.isNaN(value));
  const ms = known ? Math.max(0, Math.max(...ends) - Math.min(...starts)) : null;
  const endedAt = known ? Math.max(...ends) : null;
  return { kind: "settled", outcome, ms, endedAt, focusId };
}

/** How long a Helper the loaded feed does not list may still count as just started. */
export const UNLISTED_GRACE_MS = 60_000;

/** The Helpers the loaded feed does not list (none before it has loaded). */
export function unlistedHelperIds(
  helpers: readonly StripHelper[],
  activities: readonly Activity[] | undefined,
): string[] {
  if (!activities) return [];
  const forest = buildActivityForest(activities);
  return helpers
    .filter((helper) => !findHelperNode(forest, helper.helperId))
    .map((helper) => helper.helperId);
}

/**
 * Whether a Helper the feed does not list runs. Before the feed has loaded, the status the
 * transcript carried says. Once it has, a Helper missing from it either started a moment ago or is
 * older than the feed's newest page, and the transcript read its status once and never again: it
 * counts as running only while the turn runs or for a short grace after it was first found
 * missing, so it cannot stay lit for good.
 */
function unlistedRunning(
  helper: StripHelper,
  activities: readonly Activity[] | undefined,
  turnRunning: boolean,
  now: number,
  firstMissing: FirstMissing,
): boolean {
  if (helper.status !== "in_progress") return false;
  if (activities === undefined || turnRunning) return true;
  return now - (firstMissing.get(helper.helperId) ?? now) < UNLISTED_GRACE_MS;
}

/** What the strip knows of the reply itself (workPlan.ts), beside its Helpers. */
export interface StripTurn {
  /** The Activity of the reply's own turn, when the engine named it. */
  activityId?: string | null;
  /** The files the reply's own turn saved, by artifact id. */
  files?: string[];
  /** The reply is being written right now. */
  live: boolean;
  /** When the person asked (ms), or null when unknown. */
  startedAt: number | null;
  /** The reply's newest message (ms), or null when unknown. */
  endedAt: number | null;
  /** What the running turn is doing now ("Reading files…"), when it has no Helper to say so. */
  liveLabel?: string;
  /** How the reply ended (workPlan.ts `TurnWork.outcome`); a normal finish when unknown. */
  outcome?: TurnOutcome;
}

/** Everything the strip draws, from its Helpers and its reply. */
export interface StripView {
  /** The turn or one of its Helpers runs: the strip is lit. */
  live: boolean;
  outcome: "done" | "failed" | "cancelled";
  /** Live: time since the work began. Settled: the whole span. Null when unknown. */
  clockMs: number | null;
  /** The running Helpers' lines. */
  lines: WorkLine[];
  /** Everything the Helpers flagged, oldest first. */
  flags: string[];
  /** The latest progress sentence of the running work, for the current step. */
  progress: string | null;
  focusId: string | null;
}

/** The strip from its Helpers' state (`deriveWorkStrip`), what they flagged and the reply. */
export function composeStrip(
  state: WorkStripState,
  flags: string[],
  turn: StripTurn | undefined,
  now: number,
): StripView {
  const lines = state.kind === "working" ? state.lines : [];
  const live = state.kind === "working" || Boolean(turn?.live);
  const helpers = state.kind === "settled" ? state.outcome : "done";
  const ended = turn?.outcome ?? "done";
  const outcome =
    helpers === "failed" || ended === "failed"
      ? "failed"
      : helpers === "cancelled" || ended === "cancelled"
        ? "cancelled"
        : "done";
  const start = turn?.startedAt ?? null;
  let clockMs: number | null;
  if (live) {
    const helperSpan = lines.reduce<number | null>(
      (most, line) => (line.elapsedMs === null ? most : Math.max(most ?? 0, line.elapsedMs)),
      null,
    );
    clockMs = start !== null ? Math.max(0, serverNow(now) - start) : helperSpan;
  } else if (
    start !== null &&
    (turn?.endedAt != null || (state.kind === "settled" && state.endedAt !== null))
  ) {
    const end = Math.max(turn?.endedAt ?? 0, state.kind === "settled" ? (state.endedAt ?? 0) : 0);
    clockMs = Math.max(0, end - start);
  } else {
    clockMs = state.kind === "settled" ? state.ms : null;
  }
  const progress =
    lines.find((line) => line.progress)?.progress ?? (turn?.live ? (turn.liveLabel ?? null) : null);
  return {
    live,
    outcome,
    clockMs,
    lines,
    flags,
    progress,
    focusId: state.focusId,
  };
}

/** When a Helper's work ended: its own finish or a later one of a part it handed on. */
function lastFinish(node: ActivityNode): number {
  const ends = [Date.parse(node.activity.finishedAt ?? ""), ...node.children.map(lastFinish)];
  const known = ends.filter((end) => !Number.isNaN(end));
  return known.length > 0 ? Math.max(...known) : Number.NaN;
}

/** A live status line, not a reply. */
const progressOnly = (message: ThreadMessage) =>
  message.id.startsWith("progress:") ||
  message.blocks.every((block) => block.kind === "progress" || block.kind === "steps");

function helperBlocks(message: ThreadMessage): StripHelper[] {
  return message.blocks.flatMap((block) => (block.kind === "helper" ? [block] : []));
}

/**
 * The replies the assistant gave while earlier work was still going (ADR 0022 "Questions are
 * answered on the main turn"): the first reply to a person's message sent while Helpers an
 * earlier message started ran (or a part they handed on). Only a reply with words is marked; a
 * reply that starts Helpers of its own is new work, not an answer on the side. A reply to a
 * message that opened a Round is never marked: its Round's strip says what runs (ADR 0029).
 */
export function answeredWhileWorking(
  messages: readonly ThreadMessage[],
  activities: readonly Activity[] | undefined,
  /** The transcript says a turn runs right now. */
  turnRunning = false,
  /** The client clock (ms). */
  now = Date.now(),
  firstMissing: FirstMissing = NONE_MISSING,
): Set<string> {
  const answered = new Set<string>();
  const forest = activities ? buildActivityForest(activities) : [];
  const windows: Array<{ index: number; start: number; end: number }> = [];
  messages.forEach((message, index) => {
    if (message.role !== "bot") return;
    for (const helper of helperBlocks(message)) {
      const node = findHelperNode(forest, helper.helperId);
      if (node) {
        const start = Date.parse(node.activity.startedAt);
        const end = isNodeRunning(node) ? Infinity : lastFinish(node);
        if (!Number.isNaN(start) && !Number.isNaN(end)) windows.push({ index, start, end });
      } else if (unlistedRunning(helper, activities, turnRunning, now, firstMissing)) {
        windows.push({ index, start: Date.parse(message.createdAt), end: Infinity });
      }
    }
  });
  if (windows.length === 0) return answered;
  let personIndex = -1;
  let replied = false;
  messages.forEach((message, index) => {
    if (message.role === "user") {
      personIndex = index;
      replied = false;
      return;
    }
    if (replied || personIndex < 0 || progressOnly(message)) return;
    replied = true;
    if (messages[personIndex]?.roundId) return;
    if (!message.blocks.some((block) => block.kind === "text")) return;
    if (helperBlocks(message).length > 0) return;
    const asked = Date.parse(messages[personIndex]?.createdAt ?? "");
    if (
      windows.some(
        (window) => window.index < personIndex && asked >= window.start && asked < window.end,
      )
    ) {
      answered.add(message.id);
    }
  });
  return answered;
}
