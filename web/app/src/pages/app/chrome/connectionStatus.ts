// The context panel's identity header shows a small Connection Status line ("Connected" /
// "Connecting…") describing whether Superchat's calls are reaching the server at all — not
// whether the Muse is busy (that's `useMuseLiveState`). Derived from the last poll's
// outcome on whichever wires the panel already has, so no extra request is made just to
// answer this. "Connected" latches the moment any poll first lands, and only lets go
// after a few consecutive rounds where every poll has failed, so one dropped request
// doesn't flicker the indicator.

/** The subset of a poll's state this only needs: whether the last attempt ever landed. A
 * poll that answered `NOT_IMPLEMENTED` still reached a real server, so it counts as
 * connected too — only "still loading" or "a request failed" read as "Connecting…". */
export type PollOutcome = { status: "loading" | "ready" | "unavailable" | "error" };

export type ConnectionStatus = "connected" | "connecting";

/** Consecutive rounds every poll must fail before the latch lets go of "Connected". */
export const CONNECTION_FAILURE_THRESHOLD = 3;

export interface ConnectionLatch {
  status: ConnectionStatus;
  /** Consecutive rounds, since the last time any poll landed, where every poll given
   * failed (still loading or erroring). */
  consecutiveFailures: number;
}

export const INITIAL_CONNECTION_LATCH: ConnectionLatch = {
  status: "connecting",
  consecutiveFailures: 0,
};

function reached(poll: PollOutcome | undefined): boolean {
  return poll?.status === "ready" || poll?.status === "unavailable";
}

/**
 * One round's transition: any poll landing resets the latch straight to "Connected"
 * with no failures; otherwise the failure streak grows, and only once it reaches
 * `CONNECTION_FAILURE_THRESHOLD` does the latch let go and say "Connecting…" again.
 * Pure, so the latch's rules are unit-testable without timers or a real clock — the
 * caller drives it once per observed poll change (ContextPanel.tsx).
 */
export function nextConnectionLatch(
  prev: ConnectionLatch,
  ...polls: (PollOutcome | undefined)[]
): ConnectionLatch {
  if (polls.some(reached)) {
    return { status: "connected", consecutiveFailures: 0 };
  }
  const consecutiveFailures = prev.consecutiveFailures + 1;
  const status =
    prev.status === "connected" && consecutiveFailures < CONNECTION_FAILURE_THRESHOLD
      ? "connected"
      : "connecting";
  return { status, consecutiveFailures };
}
