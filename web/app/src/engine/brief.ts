import {
  getOmnigentLatestBrief,
  runOmnigentScheduledTask,
} from "@superchat/adapters/omnigent/browser";
import { engineClient, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";

/** `brief.*`: the person's morning brief (ADR 0037), relayed from the engine. */
export const briefRouter = {
  brief: {
    latest: os.brief.latest.handler(async () => {
      const found = await onSuperChat(getOmnigentLatestBrief(engineClient(), NO_EMAIL));
      const page = found.brief;
      return {
        taskId: found.task_id,
        enabled: found.enabled,
        page: page
          ? {
              artifactId: page.artifact_id,
              title: page.title ?? "Brief",
              savedAt: new Date(page.created_at * 1000).toISOString(),
              line: page.line ?? "",
              home: (page.home ?? []).map((line) => ({
                topicKey: line.topic_key,
                topic: line.topic,
                text: line.text,
                bold: line.bold,
              })),
            }
          : null,
      };
    }),
    refresh: os.brief.refresh.handler(async ({ input }) => {
      await onSuperChat(runOmnigentScheduledTask(engineClient(), NO_EMAIL, input.taskId));
      return { ok: true as const };
    }),
  },
};
