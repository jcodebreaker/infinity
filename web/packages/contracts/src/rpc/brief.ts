import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

/** One topic's line in Home's "This morning" panel: plain `text`, then `bold`. */
export const BriefHomeLineSchema = z.object({
  topicKey: z.string(),
  /** The topic's title as the brief read it. */
  topic: z.string(),
  text: z.string(),
  bold: z.string(),
});
export type BriefHomeLine = z.infer<typeof BriefHomeLineSchema>;

/** The newest morning brief page (ADR 0037). */
export const BriefPageSchema = z.object({
  /** Its newest version; open it in the File Studio. */
  artifactId: z.string(),
  title: z.string(),
  /** ISO timestamp the version was saved. */
  savedAt: z.string(),
  /** The first thing that needs the person, or "Nothing needs you today". */
  line: z.string(),
  /** One line per topic for Home; empty for a page written before them. */
  home: z.array(BriefHomeLineSchema).default([]),
});
export type BriefPage = z.infer<typeof BriefPageSchema>;

export const LatestBriefSchema = z.object({
  /** The brief's automation; `null` before the person has a Conversation. */
  taskId: z.string().nullable(),
  enabled: z.boolean(),
  /** `null` before the first run. */
  page: BriefPageSchema.nullable(),
});
export type LatestBrief = z.infer<typeof LatestBriefSchema>;

export const briefContract = {
  brief: {
    /** The newest brief (engine `GET /v1/me/brief/latest`, which sets it up on first use). */
    latest: oc.input(z.object({ botId: Id })).output(LatestBriefSchema),
    /** Make a new brief now (the automation's run-now). */
    refresh: oc
      .input(z.object({ botId: Id, taskId: Id }))
      .output(z.object({ ok: z.literal(true) })),
  },
};
