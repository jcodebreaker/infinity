import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

/** The proactive messages the person has not seen (engine `GET /v1/me/outbox/unread`). */
const OutboxUnreadSchema = z.object({
  count: z.number().int().nonnegative(),
  firstItemId: z.string().nullable(),
  lastSeenItemId: z.string().nullable(),
});
export type OutboxUnread = z.infer<typeof OutboxUnreadSchema>;

export const outboxContract = {
  outbox: {
    /** How many proactive messages arrived since the person last saw the Conversation. */
    unread: oc.input(z.object({ botId: Id })).output(OutboxUnreadSchema),
    /** The person saw the Conversation up to the proactive message `itemId`. */
    seen: oc.input(z.object({ botId: Id, itemId: Id })).output(OutboxUnreadSchema),
    /** *Not useful* on a proactive message: a stored rule, not a vibe (ADR 0036). */
    notUseful: oc
      .input(z.object({ botId: Id, itemId: Id, reason: z.string().max(1000).optional() }))
      .output(z.object({ ok: z.literal(true) })),
    /** Pick a proactive message's topic up in the Conversation (ADR 0035's pick); `itemId` is
     * the message it came from, which the engine counts as acted on (ADR 0038). */
    openTopic: oc
      .input(z.object({ botId: Id, topicKey: z.string().min(1).max(128), itemId: Id.optional() }))
      .output(z.object({ ok: z.literal(true) })),
  },
};
