import type { Automation, AutomationSchedule, SystemJob } from "@superchat/contracts";
import { useCallback, useEffect, useState } from "react";
import { rpc } from "../../lib/rpc";

export type AutomationsState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; automations: Automation[] };

/** The calls the sheet makes; the live RPC unless a dev preview supplies its own. */
export type AutomationsWire = Pick<
  typeof rpc.automations,
  "list" | "system" | "create" | "update" | "run" | "remove"
>;

/**
 * The live calls, made once: the RPC client hands out a new `rpc.automations` object on every
 * read, so using it as a default would change `refresh` each render and reload forever.
 */
const LIVE_WIRE: AutomationsWire = {
  list: (input) => rpc.automations.list(input),
  system: (input) => rpc.automations.system(input),
  create: (input) => rpc.automations.create(input),
  update: (input) => rpc.automations.update(input),
  run: (input) => rpc.automations.run(input),
  remove: (input) => rpc.automations.remove(input),
};

const RUNNING_POLL_MS = 3_000;

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

/** The person's automations (the engine's scheduled tasks) and the actions on them. */
export function useAutomations(botId: string, enabled: boolean, wire: AutomationsWire = LIVE_WIRE) {
  const [state, setState] = useState<AutomationsState>({ status: "loading" });

  const refresh = useCallback(async () => {
    try {
      const automations = await wire.list({ botId });
      setState({ status: "ready", automations });
    } catch (error) {
      setState((current) =>
        current.status === "ready" ? current : { status: "error", message: message(error) },
      );
    }
  }, [botId, wire]);

  useEffect(() => {
    if (!enabled) return;
    setState({ status: "loading" });
    void refresh();
  }, [enabled, refresh]);

  // While a run is going, follow it until it ends.
  const running =
    state.status === "ready" && state.automations.some((a) => a.lastRunStatus === "running");
  useEffect(() => {
    if (!enabled || !running) return;
    const poll = window.setInterval(() => void refresh(), RUNNING_POLL_MS);
    return () => window.clearInterval(poll);
  }, [enabled, running, refresh]);

  const [systemJobs, setSystemJobs] = useState<SystemJob[] | null>(null);
  // Read-only and best effort: a failure leaves the section empty rather than breaking the sheet.
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    wire
      .system({ botId })
      .then((jobs) => {
        if (!cancelled) setSystemJobs(jobs);
      })
      .catch(() => {
        if (!cancelled) setSystemJobs([]);
      });
    return () => {
      cancelled = true;
    };
  }, [botId, enabled, wire]);

  const create = useCallback(
    async (input: {
      name: string;
      prompt: string;
      schedule: AutomationSchedule;
      model?: string;
    }) => {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      await wire.create({ botId, ...input, timezone });
      await refresh();
    },
    [botId, refresh, wire],
  );
  const setEnabled = useCallback(
    async (id: string, on: boolean) => {
      await wire.update({ botId, id, enabled: on });
      await refresh();
    },
    [botId, refresh, wire],
  );
  const runNow = useCallback(
    async (id: string) => {
      await wire.run({ botId, id });
      await refresh();
    },
    [botId, refresh, wire],
  );
  const remove = useCallback(
    async (id: string) => {
      await wire.remove({ botId, id });
      await refresh();
    },
    [botId, refresh, wire],
  );

  return { state, systemJobs, refresh, create, setEnabled, runNow, remove };
}
