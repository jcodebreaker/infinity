import type { Activity, RoundSummary } from "@superchat/contracts";
import { useMemo, useSyncExternalStore } from "react";
import type { ActivitiesState } from "./activityFeed";
import { isHelperActivity, isRunning } from "./activityGrouping";
import { buildActivityForest, isNodeRunning } from "./activityTree";
import { workingSince } from "./superchatWork";
import { activityFeedFor } from "./useActivities";

// The Conversation's work state: the one reading of "is Superchat working" that the header pill,
// the work strips, the working row, the orbs and the composer's Stop all take as a prop. Fed by
// the engine only: in a Conversation that records Rounds (ADR 0029), the newest Round's status and
// start, and Pause from the Activity feed; before Rounds, the transcript's turn status and the
// Activity feed.

/** Idle, a message on its way, work running, or a Stop on its way. */
export type WorkPhase = "idle" | "sending" | "running" | "stopping";

/** What the transcript knows of the Conversation's turn (useMuseTranscript). */
export interface TurnStatus {
  /** The engine says a turn runs; false from its `turnDone`. */
  running: boolean;
  /** A reply is expected for the message just sent. */
  awaiting: boolean;
  /** A Stop landed: nothing counts as running until the turn's `turnDone` or the next send. */
  stopped: boolean;
  /** The newest turn ended stopped (a Stop, or the engine's `turnDone` said so), until the next
   * send. */
  cancelled: boolean;
}

export interface WorkState {
  phase: WorkPhase;
  /** Anything runs: the phase is not idle. */
  running: boolean;
  /** The Conversation's own turn runs (or a send or a Stop is on its way): its newest reply's strip
   * is live. */
  turnRunning: boolean;
  /** When the running work began (ms, the engine's clock), which every live clock counts from;
   * null while idle or when unknown. */
  since: number | null;
  /** The newest turn was stopped: its strip reads Cancelled. */
  cancelled: boolean;
  /** Only Helpers run, and Pause holds every one of them (ADR 0027): the pill reads Paused. */
  paused: boolean;
}

const NO_ROUNDS: readonly RoundSummary[] = [];

export const IDLE_TURN: TurnStatus = {
  running: false,
  awaiting: false,
  stopped: false,
  cancelled: false,
};

export function deriveWorkState({
  turn,
  sending,
  stopping,
  activities,
  turnStartedAt,
  rounds = NO_ROUNDS,
  roundId,
}: {
  turn: TurnStatus;
  /** The composer's message is on its way (its upload or its post). */
  sending: boolean;
  /** The composer's Stop is on its way. */
  stopping: boolean;
  /** The Activity feed, when loaded. */
  activities: readonly Activity[];
  /** When the newest turn began (ms, the engine's clock): workPlan.ts `runningSince`. */
  turnStartedAt: number | null;
  /** The Conversation's Rounds, newest first (`useRounds`); none before Rounds. */
  rounds?: readonly RoundSummary[];
  /** The Round the person's newest message opened, once it has one. */
  roundId?: string;
}): WorkState {
  const newest = rounds[0];
  if (newest) return roundWorkState({ newest, roundId, turn, sending, stopping, activities });
  const turnLive = !turn.stopped && (turn.running || turn.awaiting);
  const since = turn.stopped ? null : workingSince(turnLive ? turnStartedAt : null, activities);
  const phase: WorkPhase = stopping
    ? "stopping"
    : sending
      ? "sending"
      : turnLive || since !== null
        ? "running"
        : "idle";
  const helpers = buildActivityForest(activities).filter(
    (node) => node.activity.source !== "studio" && isNodeRunning(node),
  );
  return {
    phase,
    running: phase !== "idle",
    turnRunning: turnLive || sending || stopping,
    since: phase === "idle" ? null : since,
    cancelled: turn.cancelled,
    paused:
      phase === "running" &&
      !turnLive &&
      helpers.length > 0 &&
      helpers.every((node) => node.activity.paused === true),
  };
}

/** A Conversation that records Rounds: working while its newest Round runs, counted from that
 * Round's start. A message whose Round has not arrived yet runs on the transcript's word alone;
 * a Helper outside the Round never lights it. Paused while Pause holds every running Helper of
 * the Round. */
function roundWorkState({
  newest,
  roundId,
  turn,
  sending,
  stopping,
  activities,
}: {
  newest: RoundSummary;
  roundId: string | undefined;
  turn: TurnStatus;
  sending: boolean;
  stopping: boolean;
  activities: readonly Activity[];
}): WorkState {
  const roundRuns = newest.status === "running";
  const unopened = roundId === undefined && !turn.stopped && (turn.running || turn.awaiting);
  const phase: WorkPhase = stopping
    ? "stopping"
    : sending
      ? "sending"
      : roundRuns || unopened
        ? "running"
        : "idle";
  const helpers = roundRuns
    ? activities.filter(
        (activity) =>
          activity.roundId === newest.roundId && isHelperActivity(activity) && isRunning(activity),
      )
    : [];
  return {
    phase,
    running: phase !== "idle",
    turnRunning: phase !== "idle",
    since: roundRuns ? newest.startedAt : null,
    cancelled: turn.cancelled,
    paused:
      phase === "running" &&
      helpers.length > 0 &&
      helpers.every((activity) => activity.paused === true),
  };
}

/** The Conversation's work state (`deriveWorkState`), reading the shared Activity feed. */
export function useWorkState({
  botId,
  turn,
  sending,
  stopping,
  turnStartedAt,
  rounds,
  roundId,
}: {
  /** None before a Muse is open: nothing runs. */
  botId: string | undefined;
  turn: TurnStatus;
  sending: boolean;
  stopping: boolean;
  turnStartedAt: number | null;
  rounds: readonly RoundSummary[];
  roundId: string | undefined;
}): WorkState {
  const feed = botId ? activityFeedFor(botId) : null;
  const { state } = useSyncExternalStore(
    feed?.subscribe ?? noSubscribe,
    feed?.getSnapshot ?? noSnapshot,
  );
  const activities = state.status === "ready" ? state.activities : NO_ACTIVITIES;
  const { phase, running, turnRunning, since, cancelled, paused } = deriveWorkState({
    turn,
    sending,
    stopping,
    activities,
    turnStartedAt,
    rounds,
    roundId,
  });
  // One object while nothing it says changed: the memoised views it is handed keep still.
  return useMemo(
    () => ({ phase, running, turnRunning, since, cancelled, paused }),
    [phase, running, turnRunning, since, cancelled, paused],
  );
}

const NO_ACTIVITIES: readonly Activity[] = [];
const NO_FEED: { state: ActivitiesState } = { state: { status: "loading" } };
const noSubscribe = () => () => undefined;
const noSnapshot = () => NO_FEED;
