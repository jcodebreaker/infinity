import { eventIterator, oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

// The Activity panel (docs/glossary.md "Activity", "Activity Feed", "Step"; docs/helpers.md
// "The activity panel"): every piece of multi-step work the Muse did — a Conversation or Side
// Chat turn that used tools, or a Helper's whole task. Derived by Omnigent on read
// (`GET /v1/sessions/{id}/activities`, docs/helpers.md) and passed
// through by `activities.list`/`activities.get` (this file).

export const ActivityKindSchema = z.enum(["turn", "sub_agent"]);
export type ActivityKind = z.infer<typeof ActivityKindSchema>;

/** Where an Activity came from, for the icon beside it: a Conversation turn, background work
 * the Muse started, upkeep, a scheduled Helper (an automation), or a turn in a file's studio
 * (ADR 0024). Distinct from `kind`, which says how the engine stores it. */
export const ActivitySourceSchema = z.enum([
  "turn",
  "background",
  "housekeeping",
  "scheduled",
  "studio",
]);
export type ActivitySource = z.infer<typeof ActivitySourceSchema>;

export const ActivityStatusSchema = z.enum(["in_progress", "done", "failed", "cancelled"]);
export type ActivityStatus = z.infer<typeof ActivityStatusSchema>;

/** One action within an Activity, in plain language (docs/glossary.md "Step"). Populated only when
 * the Activity is read in full (`activities.get`), not on the list view. */
export const ActivityStepSchema = z.object({
  itemId: z.string(),
  title: z.string(),
  createdAt: z.string(),
  tool: z.string().nullable(),
  detail: z.record(z.string(), z.unknown()).nullable().optional(),
});
export type ActivityStep = z.infer<typeof ActivityStepSchema>;

/** The kinds of tool a person recognises, as the engine names them for an Activity (ADR 0027
 * "Used in this conversation"). */
export const ToolFamilySchema = z.enum(["web", "files", "decks", "shell", "browser"]);
export type ToolFamily = z.infer<typeof ToolFamilySchema>;

export const ActivitySchema = z.object({
  id: Id,
  kind: ActivityKindSchema,
  /** The chat this Activity happened in: the Conversation for a turn, or the Helper's own
   * chat for a `sub_agent` Activity. */
  chatId: Id,
  source: ActivitySourceSchema,
  /** A short action-first title ("Check Tesla's latest price"), model-written by the engine. */
  title: z.string(),
  /** One-line outcome, or null while still in progress / nothing to report. */
  outcome: z.string().nullable(),
  /** The row's one-line gist of the outcome; null while in progress (the live Step stands in)
   * or when there is none. */
  summary: z.string().nullable(),
  status: ActivityStatusSchema,
  startedAt: z.string(),
  /** Null while `status` is "in_progress". */
  finishedAt: z.string().nullable(),
  /** Calendar date (local to the Activity) the panel groups by, e.g. "2026-10-03". */
  date: z.string(),
  /** For a Helper, the chat that started it: the Conversation, or — for a part of a bigger
   * task — another Helper's chat (its `chatId`), which is how parts nest under it. */
  parentChatId: Id.nullable().optional(),
  /** A studio turn's file (`source: "studio"`): the panel groups its turns under the name, and
   * under the request that created the file (`roundId`, that Round) when it is listed. */
  artifact: z
    .object({ groupId: z.string().min(1), name: z.string(), roundId: z.string().optional() })
    .optional(),
  /** Present (possibly empty) on `activities.get`; omitted on the `activities.list` summary. */
  steps: z.array(ActivityStepSchema).optional(),
  /** What the work did, in one line under the reply ("Created 2 files, searched the web, and 12
   * more steps"); written by the engine (ADR 0027 "Display is the engine's"). */
  summaryLine: z.string().nullable().optional(),
  /** The kinds of tool the work used, as the engine names them. */
  families: z.array(ToolFamilySchema).optional(),
  /** A Helper held by Pause (ADR 0027); absent on a turn. */
  paused: z.boolean().optional(),
  /** The Round the work is in (ADR 0029): a Conversation turn's, or the one that started a
   * Helper; absent for work from before Rounds. */
  roundId: z.string().optional(),
});
export type Activity = z.infer<typeof ActivitySchema>;

export const ActivityPageSchema = z.object({
  activities: z.array(ActivitySchema),
  /** True when an older page exists; `activities.list`'s `before` input pages further back. */
  hasMore: z.boolean(),
});
export type ActivityPage = z.infer<typeof ActivityPageSchema>;

/** One frame of `activities.watch`: the feed may have changed, read it again. The first frame
 * means the watch is live; `heartbeat` only keeps the connection from idling out. */
export const ActivityChangedSchema = z.object({ type: z.enum(["changed", "heartbeat"]) });
export type ActivityChanged = z.infer<typeof ActivityChangedSchema>;

export const activityContract = {
  activities: {
    list: oc
      .input(
        z.object({
          botId: Id,
          /** Epoch seconds; only Activities started strictly before this. */
          before: z.number().int().nonnegative().optional(),
          limit: z.number().int().positive().max(100).optional(),
        }),
      )
      .output(ActivityPageSchema),
    get: oc.input(z.object({ botId: Id, activityId: z.string().min(1) })).output(ActivitySchema),
    /** Live signal: one event on connect and one each time the Activity Feed may have changed
     * (a Helper started, stepped, settled). Carries no content; the client re-reads `list`. */
    watch: oc.input(z.object({ botId: Id })).output(eventIterator(ActivityChangedSchema)),
  },
};
