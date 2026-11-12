import type { OmnigentClientConfig } from "./core.js";
import { omnigentHeaders, throwOnError } from "./core.js";

/** A scheduled task (engine/omnigent/server/routes/scheduled_tasks.py `_to_response`);
 * `parent_session_id` and `agent_type` tie a followed topic to its Muse's Super Chat. */
export interface OmnigentScheduledTask {
  id: string;
  name: string;
  prompt: string;
  rrule: string;
  timezone: string;
  state: "active" | "paused" | string;
  parent_session_id?: string | null;
  agent_type?: string | null;
  created_at?: number;
  last_run_at?: number | null;
  next_run_at?: string | null;
  /** Product marker the task was created with (e.g. `automation`, `followed_topic`). */
  kind?: string | null;
  last_run_status?: string | null;
  /** The model the task runs with; `null` uses the agent's default. */
  model_override?: string | null;
}

function scheduledTaskUrl(config: OmnigentClientConfig, id?: string, tail = ""): URL {
  const path = id ? `/v1/scheduled-tasks/${encodeURIComponent(id)}${tail}` : "/v1/scheduled-tasks";
  return new URL(path, config.baseUrl);
}

/** `GET /v1/scheduled-tasks[?parent_session_id=]` — the owner's tasks, or only those parented on
 * one Super Chat. */
export async function listOmnigentScheduledTasks(
  config: OmnigentClientConfig,
  email: string,
  parentSessionId?: string,
): Promise<OmnigentScheduledTask[]> {
  const url = scheduledTaskUrl(config);
  if (parentSessionId) url.searchParams.set("parent_session_id", parentSessionId);
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "list scheduled tasks", config.secrets);
  const body = (await response.json()) as { scheduled_tasks?: OmnigentScheduledTask[] };
  return body.scheduled_tasks ?? [];
}

export async function getOmnigentScheduledTask(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<OmnigentScheduledTask> {
  const response = await fetch(scheduledTaskUrl(config, id), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "get scheduled task", config.secrets);
  return (await response.json()) as OmnigentScheduledTask;
}

export async function createOmnigentScheduledTask(
  config: OmnigentClientConfig,
  email: string,
  input: {
    name: string;
    prompt: string;
    rrule: string;
    parentSessionId: string;
    agentType: string;
    /** Product marker, e.g. `followed_topic`. */
    kind?: string;
    timezone?: string;
    /** A model id from the engine catalog; absent uses the agent's default. */
    modelOverride?: string;
  },
): Promise<OmnigentScheduledTask> {
  const response = await fetch(scheduledTaskUrl(config), {
    method: "POST",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify({
      name: input.name,
      prompt: input.prompt,
      rrule: input.rrule,
      parent_session_id: input.parentSessionId,
      agent_type: input.agentType,
      ...(input.kind ? { kind: input.kind } : {}),
      ...(input.timezone ? { timezone: input.timezone } : {}),
      ...(input.modelOverride ? { model_override: input.modelOverride } : {}),
    }),
  });
  await throwOnError(response, "create scheduled task", config.secrets);
  return (await response.json()) as OmnigentScheduledTask;
}

/** `PATCH /v1/scheduled-tasks/{id}` — pause (`"paused"`) or resume (`"active"`), or change the
 * schedule (`rrule`). */
export async function patchOmnigentScheduledTask(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  patch: { state?: "active" | "paused"; rrule?: string; name?: string; prompt?: string },
): Promise<OmnigentScheduledTask> {
  const response = await fetch(scheduledTaskUrl(config, id), {
    method: "PATCH",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify(patch),
  });
  await throwOnError(response, "update scheduled task", config.secrets);
  return (await response.json()) as OmnigentScheduledTask;
}

/** One run of a scheduled task (`GET /v1/scheduled-tasks/{id}/runs`); times are epoch seconds.
 * `conversation_id` is the session the run created (a Helper's own chat). */
export interface OmnigentScheduledTaskRun {
  id: string;
  scheduled_task_id: string;
  /** `scheduled`, `running`, `succeeded`, `failed` or `skipped`. */
  status: string;
  scheduled_at: number | null;
  fired_at: number | null;
  finished_at: number | null;
  conversation_id: string | null;
  error_code: string | null;
}

/** `GET /v1/scheduled-tasks/{id}/runs?limit=` — the newest runs first. */
export async function listOmnigentScheduledTaskRuns(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  limit = 20,
): Promise<OmnigentScheduledTaskRun[]> {
  const url = scheduledTaskUrl(config, id, "/runs");
  url.searchParams.set("limit", String(limit));
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "list scheduled task runs", config.secrets);
  const body = (await response.json()) as { runs?: OmnigentScheduledTaskRun[] };
  return body.runs ?? [];
}

/** `POST /v1/scheduled-tasks/{id}/run` — fire now (202, fire-and-forget). */
export async function runOmnigentScheduledTask(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<void> {
  const response = await fetch(scheduledTaskUrl(config, id, "/run"), {
    method: "POST",
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "run scheduled task", config.secrets);
}

export async function deleteOmnigentScheduledTask(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<void> {
  const response = await fetch(scheduledTaskUrl(config, id), {
    method: "DELETE",
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "delete scheduled task", config.secrets);
}

/** One of the engine's own recurring background jobs (`GET /v1/me/system-jobs`,
 * engine/omnigent/superchat/system_jobs/routes.py). Times are epoch seconds. */
export interface OmnigentSystemJob {
  id: string;
  name: string;
  description: string;
  schedule: string;
  last_run_at: number | null;
  last_outcome?: string | null;
  last_failed?: boolean;
  next_run_at: number | null;
  enabled: boolean;
}

/** `GET /v1/me/system-jobs` — the engine's built-in background jobs, for this person. */
export async function listOmnigentSystemJobs(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentSystemJob[]> {
  const response = await fetch(new URL("/v1/me/system-jobs", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "list system jobs", config.secrets);
  const body = (await response.json()) as { jobs?: OmnigentSystemJob[] };
  return body.jobs ?? [];
}
