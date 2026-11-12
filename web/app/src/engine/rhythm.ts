// The rhythm (ADR 0038): the web reports what the person did with a proactive message; the
// engine learns from it when they like to hear from Superchat. Nothing here is shown.

import { ORPCError } from "@orpc/server";
import { reportOmnigentRhythmEvent } from "@superchat/adapters/omnigent/browser";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";

/** `rhythm.*`: behaviour events for the engine's rhythm. */
export const rhythmRouter = {
  rhythm: {
    report: os.rhythm.report.handler(async ({ input }) => {
      // The web's `botId` is the Conversation's id.
      if (input.botId !== (await museSessionId()))
        throw new ORPCError("NOT_FOUND", { message: "Muse not found" });
      await onSuperChat(
        reportOmnigentRhythmEvent(engineClient(), NO_EMAIL, input.itemId, input.event),
      );
      return { ok: true as const };
    }),
  },
};
