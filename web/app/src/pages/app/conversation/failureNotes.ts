import type { ThreadMessage } from "@superchat/contracts";
import { MODEL_ERROR_COPY } from "@superchat/core";

/** A Muse reply that is only a failure note (the engine's `error` block): no words of its own.
 * An info notice (`level: info`) is not a failure, so it never folds into a failure run. */
export function isFailureNote(message: ThreadMessage): boolean {
  return (
    message.role === "bot" &&
    message.blocks.length > 0 &&
    message.blocks.every((block) => block.kind === "error" && block.level !== "info")
  );
}

/** One transcript row: a message, or several failure notes in a row folded into one. */
export type TranscriptRow =
  | { kind: "message"; message: ThreadMessage }
  | { kind: "failures"; messages: ThreadMessage[] };

/**
 * The transcript as rows, with consecutive failure notes ("My host restarted. Try
 * again." three times over) folded into one `failures` row. A lone failure stays a message,
 * and anything between two failures (the person's retry, a real reply) breaks the run.
 */
export function foldFailureRuns(messages: readonly ThreadMessage[]): TranscriptRow[] {
  const rows: TranscriptRow[] = [];
  let run: ThreadMessage[] = [];
  const flush = () => {
    if (run.length === 1) rows.push({ kind: "message", message: run[0] as ThreadMessage });
    else if (run.length > 1) rows.push({ kind: "failures", messages: run });
    run = [];
  };
  for (const message of messages) {
    if (isFailureNote(message)) {
      run.push(message);
      continue;
    }
    flush();
    rows.push({ kind: "message", message });
  }
  flush();
  return rows;
}

/** The latest reply, when it failed for a model-layer reason the person can act on (a key, a
 * budget, a paused account): the composer keeps its note until the next message replaces it. */
export function latestModelFailure(
  messages: readonly ThreadMessage[],
): { id: string; code: string } | null {
  const last = messages.at(-1);
  if (!last || !isFailureNote(last)) return null;
  for (const block of last.blocks) {
    if (block.kind === "error" && block.code in MODEL_ERROR_COPY) {
      return { id: last.id, code: block.code };
    }
  }
  return null;
}
