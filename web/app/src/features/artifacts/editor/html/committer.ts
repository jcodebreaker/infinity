import type { HtmlPatch } from "@superchat/contracts";
import { coalescePatches } from "./edit-model";

/** How long staged patches wait for more changes before they become one save. */
export const COMMIT_DEBOUNCE_MS = 600;

export type CommitOutcome = { ok: true } | { ok: false; message: string };

/**
 * Batches the editor's patches into few saves. Slider drags and typing `stage()` patches (they
 * wait for a quiet moment, merged per element); a text commit, a blur or a button uses
 * `commit()` to save now. Only one save is in flight at a time, so each save builds on the
 * version the last one produced; changes made meanwhile go out in the next one.
 */
export class Committer {
  private queue: HtmlPatch[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  private flight: Promise<void> = Promise.resolve();
  private disposed = false;

  constructor(
    private readonly save: (patches: HtmlPatch[]) => Promise<CommitOutcome>,
    private readonly debounceMs: number = COMMIT_DEBOUNCE_MS,
  ) {}

  /** Wait for more changes, then save them together. */
  stage(patches: readonly HtmlPatch[]): void {
    this.queue.push(...patches);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.flush(), this.debounceMs);
  }

  /** Save the queue plus `patches` now. Resolves when everything queued so far is saved. */
  commit(patches: readonly HtmlPatch[] = []): Promise<void> {
    this.queue.push(...patches);
    return this.flush();
  }

  /** True while changes are staged or being saved. */
  get pending(): boolean {
    return this.queue.length > 0 || this.inFlight > 0;
  }

  private inFlight = 0;

  flush(): Promise<void> {
    clearTimeout(this.timer);
    this.timer = undefined;
    this.inFlight += 1;
    this.flight = this.flight.then(async () => {
      try {
        if (this.disposed || this.queue.length === 0) return;
        const batch = coalescePatches(this.queue.splice(0));
        const outcome = await this.save(batch);
        if (!outcome.ok) this.queue.length = 0; // the source moved on or refused: drop the rest
      } finally {
        this.inFlight -= 1;
      }
    });
    return this.flight;
  }

  /** Drops staged patches (a failed save reloads the frame from the source). */
  discard(): void {
    clearTimeout(this.timer);
    this.timer = undefined;
    this.queue.length = 0;
  }

  dispose(): void {
    this.disposed = true;
    clearTimeout(this.timer);
  }
}
