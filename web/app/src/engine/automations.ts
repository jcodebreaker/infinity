// `automations.*`: the person's scheduled prompts, run by the engine's scheduled tasks
// (`/v1/scheduled-tasks`). Each is a scheduled Helper task parented on the Muse's Super Chat and
// marked `automation`, so the engine fires it, owns its schedule and records its runs; this page
// keeps no rows. Only tasks bound to the Muse's Super Chat with this marker are listed, changed,
// run or removed.
import { ORPCError } from "@orpc/server";
import {
  createOmnigentScheduledTask,
  deleteOmnigentScheduledTask,
  listOmnigentScheduledTaskRuns,
  listOmnigentScheduledTasks,
  listOmnigentSystemJobs,
  type OmnigentScheduledTask,
  patchOmnigentScheduledTask,
  runOmnigentScheduledTask,
} from "@superchat/adapters/omnigent/browser";
import type { Automation, AutomationSchedule } from "@superchat/contracts";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { os } from "./context";

/** The engine's marker for an automation's scheduled task. */
const AUTOMATION_KIND = "automation";
/** The morning brief's task (ADR 0037): listed here to pause or run, never deleted. */
const BRIEF_KIND = "daily_brief";
const LISTED_KINDS = new Set([AUTOMATION_KIND, BRIEF_KIND]);
/** The Helper type an automation's runs use. */
const AUTOMATION_HELPER = "worker";

const DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] as const;
const WEEKDAYS = DAYS.slice(0, 5).join(",");

/** The form's plain choices as the engine's RRULE (evaluated in the task's timezone). */
export function scheduleToRrule(schedule: AutomationSchedule): string {
  const [hour, minute] = schedule.time.split(":").map(Number) as [number, number];
  const parts =
    schedule.frequency === "daily"
      ? ["FREQ=DAILY"]
      : schedule.frequency === "weekdays"
        ? ["FREQ=WEEKLY", `BYDAY=${WEEKDAYS}`]
        : schedule.frequency === "weekly"
          ? ["FREQ=WEEKLY", `BYDAY=${DAYS[schedule.weekday ?? 0]}`]
          : ["FREQ=MONTHLY", `BYMONTHDAY=${schedule.monthDay ?? 1}`];
  return [...parts, `BYHOUR=${hour}`, `BYMINUTE=${minute}`].join(";");
}

/** The inverse for rules `scheduleToRrule` wrote; `null` for any other rule. */
export function rruleToSchedule(rrule: string): AutomationSchedule | null {
  const fields = new Map(
    rrule
      .replace(/^RRULE:/, "")
      .split(";")
      .map((part) => part.split("=") as [string, string | undefined]),
  );
  const hour = Number(fields.get("BYHOUR"));
  const minute = Number(fields.get("BYMINUTE"));
  if (!Number.isInteger(hour) || !Number.isInteger(minute) || fields.size > 4) return null;
  const time = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  const freq = fields.get("FREQ");
  if (freq === "DAILY" && fields.size === 3) return { frequency: "daily", time };
  if (freq === "WEEKLY" && fields.get("BYDAY") === WEEKDAYS) {
    return { frequency: "weekdays", time };
  }
  if (freq === "WEEKLY") {
    const weekday = DAYS.indexOf(fields.get("BYDAY") as (typeof DAYS)[number]);
    return weekday >= 0 ? { frequency: "weekly", time, weekday } : null;
  }
  if (freq === "MONTHLY") {
    const monthDay = Number(fields.get("BYMONTHDAY"));
    return monthDay >= 1 && monthDay <= 28 ? { frequency: "monthly", time, monthDay } : null;
  }
  return null;
}

const iso = (epochSeconds: number | null | undefined) =>
  epochSeconds ? new Date(epochSeconds * 1000).toISOString() : null;

async function tasksOf(sessionId: string): Promise<OmnigentScheduledTask[]> {
  const tasks = await listOmnigentScheduledTasks(engineClient(), NO_EMAIL, sessionId);
  return tasks.filter((task) => LISTED_KINDS.has(task.kind ?? ""));
}

export function toAutomation(task: OmnigentScheduledTask): Automation {
  const enabled = task.state === "active";
  return {
    id: task.id,
    name: task.name,
    prompt: task.prompt,
    rrule: task.rrule,
    schedule: rruleToSchedule(task.rrule),
    timezone: task.timezone,
    enabled,
    nextRunAt: enabled ? (task.next_run_at ?? null) : null,
    lastRunAt: iso(task.last_run_at),
    lastRunStatus: task.last_run_status ?? null,
    model: task.model_override ?? null,
    removable: task.kind !== BRIEF_KIND,
    createdAt: iso(task.created_at) ?? new Date(0).toISOString(),
  };
}

/** The Muse's session, once the task `id` is confirmed to be one of its automations. */
async function owned(id: string): Promise<string> {
  const sessionId = await museSessionId();
  if (!(await tasksOf(sessionId)).some((task) => task.id === id)) {
    throw new ORPCError("NOT_FOUND", { message: "Automation not found" });
  }
  return sessionId;
}

export const automationsRouter = {
  automations: {
    list: os.automations.list.handler(async () =>
      (await tasksOf(await museSessionId())).map(toAutomation),
    ),
    system: os.automations.system.handler(async () =>
      (await listOmnigentSystemJobs(engineClient(), NO_EMAIL)).map((job) => ({
        id: job.id,
        name: job.name,
        description: job.description,
        schedule: job.schedule,
        lastRunAt: iso(job.last_run_at),
        lastOutcome: job.last_outcome ?? null,
        lastFailed: job.last_failed ?? false,
        nextRunAt: iso(job.next_run_at),
        enabled: job.enabled,
      })),
    ),
    create: os.automations.create.handler(async ({ input }) =>
      toAutomation(
        await createOmnigentScheduledTask(engineClient(), NO_EMAIL, {
          name: input.name,
          prompt: input.prompt,
          rrule: scheduleToRrule(input.schedule),
          parentSessionId: await museSessionId(),
          agentType: AUTOMATION_HELPER,
          kind: AUTOMATION_KIND,
          ...(input.timezone ? { timezone: input.timezone } : {}),
          ...(input.model ? { modelOverride: input.model } : {}),
        }),
      ),
    ),
    update: os.automations.update.handler(async ({ input }) => {
      await owned(input.id);
      return toAutomation(
        await patchOmnigentScheduledTask(engineClient(), NO_EMAIL, input.id, {
          ...(input.enabled === undefined ? {} : { state: input.enabled ? "active" : "paused" }),
          ...(input.name === undefined ? {} : { name: input.name }),
          ...(input.prompt === undefined ? {} : { prompt: input.prompt }),
          ...(input.schedule === undefined ? {} : { rrule: scheduleToRrule(input.schedule) }),
        }),
      );
    }),
    runs: os.automations.runs.handler(async ({ input }) =>
      (await listOmnigentScheduledTaskRuns(engineClient(), NO_EMAIL, input.id, input.limit)).map(
        (run) => ({
          id: run.id,
          status: run.status,
          scheduledAt: iso(run.scheduled_at),
          firedAt: iso(run.fired_at),
          finishedAt: iso(run.finished_at),
          errorCode: run.error_code,
          chatId: run.conversation_id,
        }),
      ),
    ),
    run: os.automations.run.handler(async ({ input }) => {
      await owned(input.id);
      await runOmnigentScheduledTask(engineClient(), NO_EMAIL, input.id);
      return { ok: true as const };
    }),
    remove: os.automations.remove.handler(async ({ input }) => {
      const sessionId = await owned(input.id);
      const task = (await tasksOf(sessionId)).find((each) => each.id === input.id);
      if (task?.kind === BRIEF_KIND) {
        throw new ORPCError("BAD_REQUEST", {
          message: "The morning brief can be paused, not deleted.",
        });
      }
      await deleteOmnigentScheduledTask(engineClient(), NO_EMAIL, input.id);
      return { ok: true as const };
    }),
  },
};
