import type { Activity } from "@superchat/contracts";
import { presentStep } from "@superchat/core";
import {
  type ActivityEntry,
  groupStudioTurns,
  isHelperActivity,
  isRunning,
} from "./activityGrouping";

// Helpers nest: a Helper that hands part of its task to others (docs/glossary.md "Helper") lists those
// parts under it, and a Helper a Round started sits under that Round's row (ADR 0029). Pure, so
// the panel row and the chat's tracker share one reading of the feed.

export interface ActivityNode {
  activity: Activity;
  /** Parts of this Helper's task, oldest first. */
  children: ActivityNode[];
}

const forests = new WeakMap<readonly Activity[], ActivityNode[]>();
const helperIndexes = new WeakMap<readonly ActivityNode[], Map<string, ActivityNode>>();

/** The feed as a forest: a Helper whose `parentChatId` is another listed Helper's chat hangs
 * under it, else under its Round's row when that is listed; everything else (turns, studio turns,
 * Helpers started from the Conversation or a Side Chat, and parts whose parent is not loaded) is
 * a root, in the order given. Built once per feed snapshot
 * (the same array gives the same forest), so every strip reading one snapshot shares it. */
export function buildActivityForest(activities: readonly Activity[]): ActivityNode[] {
  let forest = forests.get(activities);
  if (!forest) {
    forest = growForest(activities);
    forests.set(activities, forest);
  }
  return forest;
}

function growForest(activities: readonly Activity[]): ActivityNode[] {
  const nodes = new Map<string, ActivityNode>();
  const helperByChat = new Map<string, ActivityNode>();
  const roundRows = new Map<string, ActivityNode>();
  for (const activity of activities) {
    const node: ActivityNode = { activity, children: [] };
    nodes.set(activity.id, node);
    if (isHelperActivity(activity)) helperByChat.set(activity.chatId, node);
    else if (activity.roundId && activity.source !== "studio")
      roundRows.set(activity.roundId, node);
  }
  const roots: ActivityNode[] = [];
  for (const activity of activities) {
    const node = nodes.get(activity.id) as ActivityNode;
    const parent = isHelperActivity(activity)
      ? ((activity.parentChatId ? helperByChat.get(activity.parentChatId) : undefined) ??
        (activity.roundId ? roundRows.get(activity.roundId) : undefined))
      : undefined;
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  for (const node of nodes.values()) {
    node.children.sort(
      (a, b) => Date.parse(a.activity.startedAt) - Date.parse(b.activity.startedAt),
    );
  }
  return roots;
}

/** A file's studio turns under its name (groupStudioTurns). */
export type FileEntry = Extract<ActivityEntry, { kind: "file" }>;

/** A Round's row: the person's request, as opposed to a Helper or a studio turn. */
function isRequestRow(activity: Activity): boolean {
  return !isHelperActivity(activity) && activity.source !== "studio" && !!activity.roundId;
}

/** For the panel: a file's studio turns move under the request that created the file (its
 * `artifact.roundId`), when that request's row is among the roots, so one request reads as one
 * line with its work, its file and the file's later edits beneath it. Returns the roots left and
 * each request's files, by Round. Kept out of the forest, so the chat's strips never count a
 * file's edits as their Round's work. */
export function gatherRequestFiles(roots: readonly ActivityNode[]): {
  roots: ActivityNode[];
  filesByRound: Map<string, FileEntry[]>;
} {
  const requests = new Set(
    roots.flatMap((node) => (isRequestRow(node.activity) ? [node.activity.roundId] : [])),
  );
  const moved: Activity[] = [];
  const kept = roots.filter((node) => {
    const roundId = node.activity.source === "studio" ? node.activity.artifact?.roundId : undefined;
    if (!roundId || !requests.has(roundId)) return true;
    moved.push(node.activity);
    return false;
  });
  const filesByRound = new Map<string, FileEntry[]>();
  for (const entry of groupStudioTurns(moved)) {
    if (entry.kind !== "file") continue;
    const roundId = entry.activities[0]?.artifact?.roundId as string;
    const files = filesByRound.get(roundId);
    if (files) files.push(entry);
    else filesByRound.set(roundId, [entry]);
  }
  return { roots: kept, filesByRound };
}

/** What a request's row says once its own turn has settled: the Helper it handed the work to
 * finished later, so that Helper's end is the request's ("It has started on the deck." would be
 * stale); otherwise the row itself. */
export function requestEnd(node: ActivityNode): Activity {
  let end = node.activity;
  for (const child of node.children) {
    const finished = Date.parse(child.activity.finishedAt ?? "");
    if (!Number.isNaN(finished) && finished >= Date.parse(end.finishedAt ?? "")) {
      end = child.activity;
    }
  }
  return end;
}

/** The part of a node that is running right now: itself, else its first running part. */
export function runningActivity(node: ActivityNode): Activity | undefined {
  if (isRunning(node.activity)) return node.activity;
  for (const child of node.children) {
    const found = runningActivity(child);
    if (found) return found;
  }
  return undefined;
}

/** A node is running while it, or any part under it, is. */
export function isNodeRunning(node: ActivityNode): boolean {
  return isRunning(node.activity) || node.children.some(isNodeRunning);
}

/** The node for a Helper's chat, wherever it sits in the forest. The forest is indexed by
 * Helper chat once, on the first lookup. */
export function findHelperNode(
  forest: readonly ActivityNode[],
  helperChatId: string,
): ActivityNode | undefined {
  let index = helperIndexes.get(forest);
  if (!index) {
    const byChat = new Map<string, ActivityNode>();
    const visit = (node: ActivityNode) => {
      if (isHelperActivity(node.activity) && !byChat.has(node.activity.chatId)) {
        byChat.set(node.activity.chatId, node);
      }
      node.children.forEach(visit);
    };
    forest.forEach(visit);
    index = byChat;
    helperIndexes.set(forest, index);
  }
  return index.get(helperChatId);
}

/** How many Steps a Helper has taken (its own; parts count under their own row). */
export function stepCount(activity: Pick<Activity, "steps">): number {
  return activity.steps?.length ?? 0;
}

/** What an Activity is doing right now, in plain words: its latest Step's title (never a raw
 * tool name), if it has taken one yet. */
export function latestStepTitle(activity: Pick<Activity, "steps">): string | undefined {
  const step = activity.steps?.at(-1);
  return step ? presentStep(step).title : undefined;
}
