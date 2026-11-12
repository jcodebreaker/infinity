import type { ActivityStep } from "@superchat/contracts";
import {
  ARTIFACT_SAVE_TOOL,
  FLAG_TOOL,
  PROGRESS_TOOL,
  READ_TOOL,
  SHELL_TOOL,
  WRITE_TOOLS,
} from "./toolNames";

/** One line of the run page's story: a progress heading with the Steps it covers, or a
 * milestone (a flag, a saved file) that always stays in view. `heading` is null for the
 * implicit first heading over the Steps before any progress was reported. */
export type StoryItem =
  | { kind: "heading"; heading: ActivityStep | null; steps: ActivityStep[] }
  | { kind: "milestone"; step: ActivityStep };

const MILESTONE_TOOLS = new Set([FLAG_TOOL, ARTIFACT_SAVE_TOOL]);

/** A Helper's Steps as a story (ADR 0022: the activity projection in plain words): each
 * `report_progress` Step heads the Steps after it, until the next one. Flags and saved files
 * stay at the top level, after the heading they fall under. A run that never reported
 * progress has no story (null): it keeps the flat list. */
export function stepStory(steps: ActivityStep[]): StoryItem[] | null {
  if (!steps.some((step) => step.tool === PROGRESS_TOOL)) return null;
  const items: StoryItem[] = [];
  let current: Extract<StoryItem, { kind: "heading" }> | null = null;
  const pending: StoryItem[] = [];
  const flush = () => items.push(...pending.splice(0));
  for (const step of steps) {
    if (step.tool === PROGRESS_TOOL) {
      flush();
      current = { kind: "heading", heading: step, steps: [] };
      items.push(current);
    } else if (step.tool && MILESTONE_TOOLS.has(step.tool)) {
      (current ? pending : items).push({ kind: "milestone", step });
    } else {
      if (!current) {
        current = { kind: "heading", heading: null, steps: [] };
        items.push(current);
      }
      current.steps.push(step);
    }
  }
  flush();
  return items;
}

export type StepTally = { commands: number; files: number; looked: number; other: number };

/** What a heading covers, by kind: shell commands, files written or edited, files read, and
 * everything else. */
export function tallySteps(steps: ActivityStep[]): StepTally {
  const tally: StepTally = { commands: 0, files: 0, looked: 0, other: 0 };
  for (const step of steps) {
    if (step.tool === SHELL_TOOL) tally.commands += 1;
    else if (step.tool && WRITE_TOOLS.has(step.tool)) tally.files += 1;
    else if (step.tool === READ_TOOL) tally.looked += 1;
    else tally.other += 1;
  }
  return tally;
}
