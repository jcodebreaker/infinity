import type { MessageBlock, MuseState } from "@superchat/contracts";
import { museAvatarState } from "@superchat/ui-web";

/**
 * The minimal shape of a live run needed to derive the Muse's face/pill state. Any
 * run record with a `status` (e.g. `@superchat/contracts` `Run`) satisfies this.
 */
export interface MuseStateRun {
  status: string;
}

// When more than one run is active at once (only really happens in a group thread),
// the "most alive" status wins — that is what the face should reflect.
const RUN_STATUS_PRIORITY: readonly string[] = ["running", "waiting_input", "leased", "queued"];

function dominantRunStatus(runs: readonly MuseStateRun[]): string | undefined {
  for (const status of RUN_STATUS_PRIORITY) {
    if (runs.some((run) => run.status === status)) return status;
  }
  return undefined;
}

/**
 * One source of truth for the live Muse state (the old design notes): derived from the
 * live run data — never a bot row's `status`, which is a snapshot that rarely reflects
 * an in-flight run — plus the open-Ask count, which always wins over an active run.
 * Reuses `museAvatarState`'s idle/thinking/working/waiting rules for a single status.
 */
export function deriveMuseState(runs: readonly MuseStateRun[], waitingCount: number): MuseState {
  return museAvatarState(dominantRunStatus(runs), waitingCount);
}

/**
 * The most recently mentioned tool name in a live progress message's blocks — the raw
 * name while it is still pending narration, the humanized `steps` label once flushed.
 * `undefined` once the run has produced no tool activity yet (e.g. still thinking).
 */
export function currentToolName(blocks: readonly MessageBlock[] | undefined): string | undefined {
  const tail = blocks?.at(-1);
  if (!tail) return undefined;
  if (tail.kind === "progress") return tail.pendingToolNames?.at(-1);
  if (tail.kind === "steps") return tail.steps.at(-1)?.label;
  return undefined;
}

// Ordered, most-specific-first; the first matching keyword in the normalized
// (lowercased, non-letters stripped) tool name wins. Covers the built-in tools
// described in web/packages/adapters/src/pi-runtime.ts's describeToolActivity.
const VERB_RULES: ReadonlyArray<readonly [string, string]> = [
  ["write", "Writing a file"],
  ["attach", "Attaching a file"],
  ["list", "Listing files"],
  ["read file", "Reading a file"],
  ["open", "Opening a file"],
  ["render", "Making a chart"],
  ["plot", "Making a chart"],
  ["mcp", "Connecting a tool"],
  ["browser", "Browsing"],
  ["computer", "Using your machine"],
  ["subagent", "Running a task"],
  ["space", "Setting things up"],
  ["remember", "Saving a note"],
  ["search", "Searching the web"],
  ["fetch", "Reading a page"],
  ["skill", "Updating a skill"],
  ["shell", "Running code"],
];

/**
 * Maps a raw or humanized tool name to a short present-continuous verb for the
 * header/sidebar/gutter (e.g. "write_file" or "Write file" -> "Writing a file…").
 * Unknown or missing tools fall back to a generic "Working…".
 */
export function activityVerb(name: string | null | undefined): string {
  if (name) {
    const normalized = name
      .toLowerCase()
      .replace(/[^a-z]+/g, " ")
      .trim();
    for (const [keyword, verb] of VERB_RULES) {
      if (normalized.includes(keyword)) return `${verb}…`;
    }
  }
  return "Working…";
}

/**
 * The single label shown beside the live face everywhere it appears (header,
 * sidebar, transcript gutter, bottom-of-transcript row): `undefined` while idle,
 * where every surface shows nothing rather than a stale "Idle" caption.
 */
export function museActivityLabel(state: MuseState, toolName?: string | null): string | undefined {
  if (state === "waiting") return "Needs you";
  if (state === "thinking") return "Thinking…";
  if (state === "working") return activityVerb(toolName);
  return undefined;
}
