import { ORPCError } from "@orpc/server";
import {
  listOmnigentTopics,
  type OmnigentTopic,
  pickOmnigentTopic,
} from "@superchat/adapters/omnigent/browser";
import type { Topic, TopicCovered, TopicOpen } from "@superchat/contracts";
import { TOPIC_COVERED_KINDS, TOPIC_OPEN_KINDS } from "@superchat/contracts";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";

function mapTopic(topic: OmnigentTopic): Topic {
  return {
    key: topic.key,
    title: topic.title,
    leftOff: topic.left_off,
    covered: topic.covered.filter((c): c is TopicCovered =>
      (TOPIC_COVERED_KINDS as readonly string[]).includes(c.kind),
    ),
    open: topic.open.filter((o): o is TopicOpen =>
      (TOPIC_OPEN_KINDS as readonly string[]).includes(o.kind),
    ),
    starters: topic.starters,
    events: topic.events,
    sources: topic.sources,
    since: topic.since,
    sinceAt: topic.since_at,
    tag: topic.tag ?? null,
    statusLine: topic.status_line ?? null,
    prepared: topic.prepared?.item_id
      ? {
          itemId: topic.prepared.item_id,
          artifactId: topic.prepared.artifact_id,
          title: topic.prepared.title,
          line: topic.prepared.line,
          at: topic.prepared.at,
        }
      : null,
    workedAt: topic.worked_at ?? null,
    lastTouchedAt: topic.last_touched_at,
  };
}

/** `topics.*`: the person's topics, relayed from the engine (ADR 0035). */
export const topicsRouter = {
  topics: {
    list: os.topics.list.handler(async ({ input }) => {
      const topics = await onSuperChat(listOmnigentTopics(engineClient(), NO_EMAIL, input.days));
      return { topics: topics.map(mapTopic) };
    }),
    pick: os.topics.pick.handler(async ({ input }) => {
      // The web's `botId` is the Conversation's session id.
      const sessionId = await museSessionId();
      if (input.botId !== sessionId)
        throw new ORPCError("NOT_FOUND", { message: "Muse not found" });
      await onSuperChat(pickOmnigentTopic(engineClient(), NO_EMAIL, input.key, sessionId));
      return {};
    }),
  },
};
