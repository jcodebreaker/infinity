// The outbox (ADR 0036): proactive messages the person has not seen, and what they say about
// one. The web is one channel: it reads the unread count and reports where the person got to.

import { ORPCError } from "@orpc/server";
import {
  getOmnigentOutboxUnread,
  markOmnigentOutboxNotUseful,
  markOmnigentOutboxSeen,
  type OmnigentOutboxUnread,
  pickOmnigentTopic,
} from "@superchat/adapters/omnigent/browser";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";

function mapUnread(unread: OmnigentOutboxUnread) {
  return {
    count: unread.count,
    firstItemId: unread.first_item_id,
    lastSeenItemId: unread.last_seen_item_id,
  };
}

/** `outbox.*`: the web channel of the engine's outbox. */
export const outboxRouter = {
  outbox: {
    unread: os.outbox.unread.handler(async () =>
      mapUnread(await onSuperChat(getOmnigentOutboxUnread(engineClient(), NO_EMAIL))),
    ),
    seen: os.outbox.seen.handler(async ({ input }) =>
      mapUnread(await onSuperChat(markOmnigentOutboxSeen(engineClient(), NO_EMAIL, input.itemId))),
    ),
    notUseful: os.outbox.notUseful.handler(async ({ input }) => {
      await onSuperChat(
        markOmnigentOutboxNotUseful(engineClient(), NO_EMAIL, input.itemId, input.reason),
      );
      return { ok: true as const };
    }),
    openTopic: os.outbox.openTopic.handler(async ({ input }) => {
      // The same pick as Home's topics (ADR 0035): the web's `botId` is the Conversation's id.
      const sessionId = await museSessionId();
      if (input.botId !== sessionId)
        throw new ORPCError("NOT_FOUND", { message: "Muse not found" });
      await onSuperChat(
        pickOmnigentTopic(engineClient(), NO_EMAIL, input.topicKey, sessionId, input.itemId),
      );
      return { ok: true as const };
    }),
  },
};
