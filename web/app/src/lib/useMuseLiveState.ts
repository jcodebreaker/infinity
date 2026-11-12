import { useLingui } from "@lingui/react/macro";
import type { MuseState, Run, ThreadMessage } from "@superchat/contracts";
import { useEffect, useMemo } from "react";
import { notifyAsksChanged, useAsks } from "../features/approvals";
import { useHostLaunch } from "../features/files/useHostLaunch";
import { currentToolName, deriveMuseState, museActivityLabel } from "./museState";

/** A run as the Muse surfaces see it. */
export type MuseLiveRun = Pick<Run, "id" | "status">;

export interface MuseLiveState {
  state: MuseState;
  /** `undefined` while idle — every surface shows nothing rather than a caption. */
  label: string | undefined;
  askCount: number;
}

/** What the open Asks depend on in the live data: runs waiting on the person and the open
 * ask blocks. It changes when an approval is raised, re-raised after a restart, or closed. */
export function waitingKey(
  runs: readonly MuseLiveRun[],
  messages: readonly ThreadMessage[] | undefined,
): string {
  const waiting = runs.filter((run) => run.status === "waiting_input").map((run) => run.id);
  const open = (messages ?? []).flatMap((message) =>
    message.blocks.flatMap((block, index) =>
      block.kind === "ask" && block.status !== "answered" ? [`${message.id}#${index}`] : [],
    ),
  );
  return [...waiting, ...open].join(",");
}

/** The turn in flight in a chat, as the live-state surfaces (`useMuseLiveState`) read a run:
 * none while idle. `key` names the chat. How long it has run is the work state's `since`. */
export function useTurnRuns(key: string, running: boolean): MuseLiveRun[] {
  return useMemo<MuseLiveRun[]>(
    () => (running ? [{ id: key, status: "running" }] : []),
    [running, key],
  );
}

/** The message the Muse is writing now: what comes after the person's last one, if anything. */
function liveMessage(messages: readonly ThreadMessage[] | undefined): ThreadMessage | undefined {
  const last = messages?.at(-1);
  return last && last.role !== "user" ? last : undefined;
}

function activeRunFor(runs: readonly MuseLiveRun[]): MuseLiveRun | undefined {
  return (
    runs.find((run) => run.status === "running") ??
    runs.find((run) => run.status === "waiting_input")
  );
}

/**
 * Standalone derivation of the live Muse state (the old design notes), self-sufficient
 * beyond the live run/message data every muse-mode surface already has: the sidebar's
 * top card, the Conversation header's live label, the transcript's gutter face, and
 * its bottom-of-transcript "thinking/working" row all call this directly instead of
 * threading a precomputed state through Shell's already-large prop lists.
 */
export function useMuseLiveState({
  botId,
  runs,
  messages,
}: {
  botId: string;
  runs: readonly MuseLiveRun[];
  messages: readonly ThreadMessage[] | undefined;
}): MuseLiveState {
  const { t } = useLingui();
  const { count: askCount } = useAsks(botId);
  const waiting = waitingKey(runs, messages);
  // The open Asks live on the server; refetch them when the live data says they changed.
  useEffect(() => {
    if (botId) notifyAsksChanged(botId);
  }, [botId, waiting]);
  const state = deriveMuseState(runs, askCount);
  const busy = state === "thinking" || state === "working";
  const launch = useHostLaunch(botId, busy);
  const activeRun = activeRunFor(runs);
  const activityMessage = activeRun ? liveMessage(messages) : undefined;
  // While the host is starting nothing else is happening, so say that instead of "Thinking…".
  const starting = busy && launch !== undefined && launch.stage !== "failed";
  const label = starting
    ? t`Starting your machine…`
    : museActivityLabel(state, currentToolName(activityMessage?.blocks));
  return { state, label, askCount };
}
