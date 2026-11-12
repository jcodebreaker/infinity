import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

// Topics (ADR 0035): what the person worked on, kept by the engine's quiet-moment pass. The web
// shows them on Home and in the Topics panel; picking one adds its card to the Conversation.

export const TOPIC_COVERED_KINDS = ["Decided", "Made", "Found", "Asked"] as const;
export const TOPIC_OPEN_KINDS = ["Open", "Promised"] as const;

const Line = z.string().max(400);

export const TopicCoveredSchema = z.object({
  /** Local day, `YYYY-MM-DD`. */
  date: z.string(),
  kind: z.enum(TOPIC_COVERED_KINDS),
  text: Line,
  file: Line.optional(),
});
export type TopicCovered = z.infer<typeof TopicCoveredSchema>;

export const TopicOpenSchema = z.object({
  /** Stable across rewordings (ADR 0038); the engine assigns it. */
  id: z.string().max(16).optional(),
  kind: z.enum(TOPIC_OPEN_KINDS),
  text: Line,
  /** Local day it is due, `YYYY-MM-DD`, when the conversation gave one. */
  due: z.string().max(10).optional(),
  /** The request that takes the line on, in the person's voice; fills the composer, unsent. */
  starter: Line.optional(),
  /** The starter's button, 1 or 2 words ("Draft it", "Fix"). */
  verb: z.string().max(40).optional(),
});
export type TopicOpen = z.infer<typeof TopicOpenSchema>;

export const TopicSourcesSchema = z.object({
  chats: z.number().int().nonnegative().optional(),
  files: z.number().int().nonnegative().optional(),
  emails: z.number().int().nonnegative().optional(),
});
export type TopicSources = z.infer<typeof TopicSourcesSchema>;

/** A draft the Lookout prepared on the topic, served while its message is unseen (ADR 0038). */
export const TopicPreparedSchema = z.object({
  /** The message that carries it. */
  itemId: z.string(),
  /** The file version the draft is; open it in the File Studio. */
  artifactId: z.string().optional(),
  title: z.string().optional(),
  /** The message, as written. */
  line: z.string().optional(),
  at: z.number().optional(),
});
export type TopicPrepared = z.infer<typeof TopicPreparedSchema>;

/** One topic (engine `GET /v1/me/topics`). Times are epoch seconds. */
export const TopicSchema = z.object({
  key: z.string(),
  title: Line,
  leftOff: Line,
  covered: z.array(TopicCoveredSchema),
  open: z.array(TopicOpenSchema),
  starters: z.array(Line),
  /** Dated one-line moments, newest first: the Topics panel's by-day list. */
  events: z.array(z.object({ at: z.number(), line: Line })),
  sources: TopicSourcesSchema,
  /** What changed since (topic watch), shown as "New" while it is fresh. */
  since: Line.nullable(),
  sinceAt: z.number().nullable(),
  /** A short name for Home (1 or 2 words), when the writer gave one. */
  tag: Line.nullable().optional(),
  /** One short sentence: what needs the person, or that it is done. */
  statusLine: Line.nullable().optional(),
  /** The Lookout's unopened draft on this topic. */
  prepared: TopicPreparedSchema.nullable().optional(),
  /** When the person last worked on it: picked it up, or the Conversation wrote it. */
  workedAt: z.number().nullable().optional(),
  lastTouchedAt: z.number(),
});
export type Topic = z.infer<typeof TopicSchema>;

export const topicsContract = {
  topics: {
    /** The person's open topics touched within `days` (at most 7), newest first. */
    list: oc
      .input(z.object({ botId: Id, days: z.number().int().min(1).max(7).default(7) }))
      .output(z.object({ topics: z.array(TopicSchema) })),
    /** Pick one up: the engine adds its card to the Conversation; nothing is sent. */
    pick: oc.input(z.object({ botId: Id, key: z.string().min(1).max(64) })).output(z.object({})),
  },
};
