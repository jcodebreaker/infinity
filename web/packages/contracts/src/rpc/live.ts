import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";
import { AutomationScheduleSchema } from "./automations.js";

/** When to tell the person in chat about an update. */
export const LiveTellSchema = z.enum(["notable", "every", "never"]);
export type LiveTell = z.infer<typeof LiveTellSchema>;

const LiveChangeSchema = z.object({
  id: z.string(),
  before: z.string(),
  after: z.string(),
  source: z.string().nullable(),
  notable: z.boolean(),
});

/** A part the person changed, next to what the source says now. */
const LiveSuggestionSchema = z.object({
  id: z.string(),
  yours: z.string(),
  sourceValue: z.string(),
  source: z.string().nullable(),
});

export const LiveSchema = z.object({
  /** The schedule as the form's choices; `null` for a rule those choices cannot express. */
  schedule: AutomationScheduleSchema.nullable(),
  /** False while the schedule is paused (two updates in a row could not place their changes). */
  active: z.boolean(),
  sources: z.array(z.string()),
  tell: LiveTellSchema,
  followPublish: z.boolean(),
  /** Also update a couple of minutes after a source file changes. */
  onChange: z.boolean(),
  /** The person's own words on what to update, read by every run. */
  note: z.string(),
  /** An update the published copy held back (it has suggestions, changes that could not be
   * placed, or sits on unpublished hand edits) until the person publishes it. */
  held: z
    .object({
      version: z.number().int(),
      reason: z.enum(["suggestions", "unplaced", "edits"]),
    })
    .nullable(),
  /** Epoch seconds. */
  checkedAt: z.number().nullable(),
  openIssue: z.string().nullable(),
  lastUpdate: z
    .object({
      version: z.number().int().nullable(),
      applied: z.array(LiveChangeSchema),
      suggestions: z.array(LiveSuggestionSchema),
      at: z.number(),
    })
    .nullable(),
});
export type Live = z.infer<typeof LiveSchema>;
export type LiveChange = z.infer<typeof LiveChangeSchema>;
export type LiveSuggestion = z.infer<typeof LiveSuggestionSchema>;

/** A live page as the Automations list and the Library know it (one engine call for all pages). */
export const LivePageSchema = z.object({
  /** The page's newest version. */
  artifactId: Id,
  name: z.string(),
  title: z.string().nullable(),
  /** The page's Automation (its scheduled task); its runs are listed with `automations.runs`. */
  taskId: Id,
  schedule: AutomationScheduleSchema.nullable(),
  /** Also updates when a source file changes in the host. */
  onChange: z.boolean(),
  sources: z.array(z.string()),
  /** Epoch seconds. */
  checkedAt: z.number().nullable(),
  /** Failed runs in a row. */
  failures: z.number().int(),
  lastFailure: z.object({ at: z.number(), reason: z.string() }).nullable(),
  /** The schedule is paused (three failed runs, or two updates that could not place changes). */
  paused: z.boolean(),
  openIssue: z.string().nullable(),
  lastUpdate: z
    .object({
      version: z.number().int().nullable(),
      at: z.number().nullable(),
      changes: z.number().int(),
    })
    .nullable(),
});
export type LivePage = z.infer<typeof LivePageSchema>;

const target = z.object({ artifactId: Id });
const newVersion = z.object({ id: Id, version: z.number().int() });

export const liveContract = {
  live: {
    get: oc.input(target).output(LiveSchema.nullable()),
    /** Keep the page updated (create or change); fields left out keep their value. */
    save: oc
      .input(
        target.extend({
          schedule: AutomationScheduleSchema,
          sources: z.array(z.string().trim().min(1).max(2048)).max(50).optional(),
          tell: LiveTellSchema.optional(),
          followPublish: z.boolean().optional(),
          onChange: z.boolean().optional(),
          note: z.string().max(1000).optional(),
        }),
      )
      .output(LiveSchema.nullable()),
    /** Every live page of the person. */
    pages: oc.input(z.object({})).output(z.array(LivePageSchema)),
    /** Save the same settings again, which turns a paused page's updates back on. */
    resume: oc.input(target).output(LiveSchema.nullable()),
    /** Stop updating; the versions stay. */
    stop: oc.input(target).output(z.object({ ok: z.literal(true) })),
    /** Start fresh: forget what updates wrote and declined; keep the settings. */
    reset: oc.input(target).output(LiveSchema.nullable()),
    /** Update now. */
    run: oc.input(target).output(z.object({ ok: z.literal(true) })),
    /** Publish the update the published copy held back. */
    publish: oc.input(target).output(LiveSchema.nullable()),
    /** Undo the newest update (only while it is still the newest version). */
    undo: oc.input(target).output(newVersion),
    /** Take the source's value for a part the person had changed. */
    accept: oc.input(target.extend({ elementId: z.string().min(1) })).output(newVersion),
  },
};
