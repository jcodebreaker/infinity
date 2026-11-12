import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

/** What the person did with a proactive message (ADR 0038): the rhythm's only input. */
export const RhythmEventSchema = z.enum(["shown", "opened", "acted_on"]);
export type RhythmEvent = z.infer<typeof RhythmEventSchema>;

export const rhythmContract = {
  rhythm: {
    /** Report one behaviour event on the proactive message `itemId` (engine
     * `POST /v1/me/rhythm/events`). Never shown to the person. */
    report: oc
      .input(z.object({ botId: Id, itemId: Id, event: RhythmEventSchema }))
      .output(z.object({ ok: z.literal(true) })),
  },
};
