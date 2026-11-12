import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

/** The plain choices the "New automation" form offers; the API turns them into an RRULE. */
export const AutomationScheduleSchema = z.object({
  frequency: z.enum(["daily", "weekdays", "weekly", "monthly"]),
  /** Local time of day, `HH:MM` (24h), in the automation's timezone. */
  time: z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$/),
  /** Weekly: 0 = Monday … 6 = Sunday. */
  weekday: z.number().int().min(0).max(6).optional(),
  /** Monthly: day of the month, 1-28 so every month has it. */
  monthDay: z.number().int().min(1).max(28).optional(),
});
export type AutomationSchedule = z.infer<typeof AutomationScheduleSchema>;

export const AutomationSchema = z.object({
  id: Id,
  name: z.string(),
  prompt: z.string(),
  /** The engine's recurrence rule. */
  rrule: z.string(),
  /** The rule as the form's choices; `null` for a rule those choices cannot express. */
  schedule: AutomationScheduleSchema.nullable(),
  timezone: z.string(),
  enabled: z.boolean(),
  /** ISO timestamp from the engine's scheduler; `null` while paused. */
  nextRunAt: z.string().nullable(),
  lastRunAt: z.string().nullable(),
  /** `succeeded`, `failed`, `running`, ...; `null` before the first run. */
  lastRunStatus: z.string().nullable(),
  /** The model it runs with; `null` is the default model. */
  model: z.string().nullable(),
  /** `false` for one the app keeps for the person (the morning brief): paused, never deleted. */
  removable: z.boolean().optional(),
  createdAt: z.string(),
});
export type Automation = z.infer<typeof AutomationSchema>;

/** One of the engine's own recurring background jobs, shown read-only beside the person's. */
export const SystemJobSchema = z.object({
  id: z.string(),
  name: z.string(),
  /** One line on what it does for the person. */
  description: z.string(),
  /** Plain words, e.g. "Every hour". */
  schedule: z.string(),
  /** ISO timestamps; `null` when the engine does not know. */
  lastRunAt: z.string().nullable(),
  /** What the last run did, in plain words ("Read 4 messages · nothing new to remember"). */
  lastOutcome: z.string().nullable(),
  lastFailed: z.boolean(),
  nextRunAt: z.string().nullable(),
  enabled: z.boolean(),
});
export type SystemJob = z.infer<typeof SystemJobSchema>;

/** One run of an Automation (the engine's scheduled task run). */
export const AutomationRunSchema = z.object({
  id: Id,
  /** `scheduled`, `running`, `succeeded`, `failed` or `skipped`. */
  status: z.string(),
  /** ISO timestamps; `null` when the run has not fired or finished. */
  scheduledAt: z.string().nullable(),
  firedAt: z.string().nullable(),
  finishedAt: z.string().nullable(),
  errorCode: z.string().nullable(),
  /** The Helper's own chat, the same id as its Activity (`null` when the run never started one). */
  chatId: z.string().nullable(),
});
export type AutomationRun = z.infer<typeof AutomationRunSchema>;

const botInput = z.object({ botId: Id });
const automationInput = z.object({ botId: Id, id: Id });
const text = {
  name: z.string().trim().min(1).max(120),
  prompt: z.string().trim().min(1).max(8000),
};

export const automationsContract = {
  automations: {
    list: oc.input(botInput).output(z.array(AutomationSchema)),
    /** The engine's built-in background jobs (memory upkeep, the daily note, ...). */
    system: oc.input(botInput).output(z.array(SystemJobSchema)),
    create: oc
      .input(
        z.object({
          botId: Id,
          ...text,
          schedule: AutomationScheduleSchema,
          /** IANA timezone of the person's browser; the engine's default when absent. */
          timezone: z.string().min(1).max(64).optional(),
          /** A model id from the engine catalog; absent runs with the default model. */
          model: z.string().min(1).max(200).optional(),
        }),
      )
      .output(AutomationSchema),
    /** Pause or resume, rename, or change the prompt or schedule. */
    update: oc
      .input(
        z.object({
          botId: Id,
          id: Id,
          enabled: z.boolean().optional(),
          name: text.name.optional(),
          prompt: text.prompt.optional(),
          schedule: AutomationScheduleSchema.optional(),
        }),
      )
      .output(AutomationSchema),
    /** The newest runs of an Automation or a live page's update, newest first. */
    runs: oc
      .input(z.object({ botId: Id, id: Id, limit: z.number().int().min(1).max(50).optional() }))
      .output(z.array(AutomationRunSchema)),
    /** Fire now (fire-and-forget on the engine). */
    run: oc.input(automationInput).output(z.object({ ok: z.literal(true) })),
    remove: oc.input(automationInput).output(z.object({ ok: z.literal(true) })),
  },
};
