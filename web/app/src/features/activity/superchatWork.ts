import type { Activity, ThreadMessage } from "@superchat/contracts";
import { buildActivityForest, isNodeRunning } from "./activityTree";

/** When the work running now began (ms): the earliest of the turn's start and the running
 * Activities' starts; null when nothing runs. A file's studio turn is the studio's own work, never
 * the Conversation's (ADR 0024). */
export function workingSince(
  turnStartedAt: number | null,
  activities: readonly Activity[],
): number | null {
  const starts = buildActivityForest(activities)
    .filter((node) => node.activity.source !== "studio" && isNodeRunning(node))
    .map((node) => Date.parse(node.activity.startedAt))
    .filter((ms) => !Number.isNaN(ms));
  if (turnStartedAt !== null) starts.push(turnStartedAt);
  return starts.length > 0 ? Math.min(...starts) : null;
}

/** The client clock minus the engine's, estimated from the messages that arrived while a
 * conversation was open (the smallest gap seen, never below zero); 0 until one has. Live clocks
 * count `serverNow()` against the engine's own timestamps, so a strip and the header pill read
 * the same time before and after a reload, and settling does not jump by the skew. */
const serverClock = {
  offsetMs: null as number | null,
  threadId: undefined as string | undefined,
  seen: new Set<string>(),
};

/** Note the transcript as it is now: a message not seen before (sent or received while the
 * conversation is open) tells how far the client clock runs ahead of the engine's. */
export function noteServerClock(messages: readonly ThreadMessage[], now = Date.now()): void {
  const threadId = messages[0]?.threadId;
  if (threadId !== serverClock.threadId) {
    // A conversation just opened: what it already holds is old, not news.
    serverClock.threadId = threadId;
    serverClock.seen = new Set(messages.map((message) => message.id));
    return;
  }
  for (const message of messages) {
    if (serverClock.seen.has(message.id) || message.id === "optimistic") continue;
    serverClock.seen.add(message.id);
    const at = Date.parse(message.createdAt);
    if (Number.isNaN(at)) continue;
    const offset = Math.max(0, now - at);
    serverClock.offsetMs = Math.min(serverClock.offsetMs ?? offset, offset);
  }
}

/** The engine's clock now, from the client's `now`. */
export function serverNow(now: number): number {
  return now - (serverClock.offsetMs ?? 0);
}

/** Forget the estimate (tests, and a reload starts fresh anyway). */
export function resetServerClock(): void {
  serverClock.offsetMs = null;
  serverClock.threadId = undefined;
  serverClock.seen = new Set();
}
