import { getOmnigentHost } from "@superchat/adapters/omnigent/browser";
import type { HostStatus } from "@superchat/contracts";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { os } from "./context";

const LAUNCH_STAGES = new Set(["provisioning", "cloning", "starting", "connecting", "failed"]);

/** Whether the engine's host is ready and how its launch is going. The page keeps no host
 * state: the engine owns it and is always on. */
async function hostStatus(botId: string): Promise<HostStatus> {
  const { ready, launch } = await getOmnigentHost(engineClient(), NO_EMAIL, await museSessionId());
  // Running means the Conversation's runner is bound and connected.
  const status: HostStatus = {
    botId,
    state: ready ? "running" : "stopped",
    runnerReady: ready,
  };
  const stage = launch?.stage;
  return stage && stage !== "ready" && LAUNCH_STAGES.has(stage)
    ? { ...status, launch: { stage: stage as NonNullable<HostStatus["launch"]>["stage"] } }
    : status;
}

export const hostRouter = {
  host: {
    status: os.host.status.handler(({ input }) => hostStatus(input.botId)),
  },
};
