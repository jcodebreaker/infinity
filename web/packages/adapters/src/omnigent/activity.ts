// The Activity panel (docs/glossary.md "Activity", "Activity Feed", "Step"; docs/helpers.md
// "The activity panel"): the Omnigent client for the Activity Feed, and the mapping from
// Omnigent's snake_case Activity/Step shape (engine/omnigent/superchat/activity/) to
// Superchat's camelCase contract (web/packages/contracts/src/rpc/activity.ts). The mapping is pure:
// no network/database, so ./activity.test.ts exercises it directly.
import type { Activity, ActivityStep, ToolFamily } from "@superchat/contracts";
import { ToolFamilySchema } from "@superchat/contracts";
import { isEngineErrorText } from "@superchat/core";
import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";
import { type OmnigentPaginatedList, streamOmnigentSse } from "./client/sessions.js";

export interface OmnigentActivityStep {
  item_id: string;
  title: string;
  created_at: number;
  tool?: string | null;
  detail?: Record<string, unknown> | null;
}

export interface OmnigentActivity {
  id: string;
  kind: "turn" | "sub_agent";
  source: "turn" | "background" | "housekeeping" | "scheduled" | "studio";
  chat_id: string;
  title: string;
  outcome: string | null;
  summary: string | null;
  status: "in_progress" | "done" | "failed" | "cancelled";
  started_at: number;
  finished_at: number | null;
  date: string;
  steps?: OmnigentActivityStep[];
  parent_chat_id?: string | null;
  /** A studio turn's file (ADR 0024). */
  artifact?: { group_id: string; name: string; round_id?: string | null } | null;
  /** ADR 0027: the engine's one line of what the work did, and the kinds of tool it used. */
  summary_line?: string | null;
  families?: string[];
  /** A Helper's entry: whether Pause holds it (its `superchat.paused` label). */
  paused?: unknown;
  /** The Round the work is in (ADR 0029); null for work from before Rounds. */
  round_id?: string | null;
}

/** `GET /v1/sessions/{superId}/activities?before=&limit=` — the Activity Feed, newest first
 * (superside-chat.md). */
export async function listOmnigentActivities(
  config: OmnigentClientConfig,
  email: string,
  superSessionId: string,
  options: { before?: number; limit?: number; tz?: string } = {},
): Promise<OmnigentPaginatedList<OmnigentActivity>> {
  const url = new URL(
    `/v1/sessions/${encodeURIComponent(superSessionId)}/activities`,
    config.baseUrl,
  );
  if (options.before !== undefined) url.searchParams.set("before", String(options.before));
  if (options.limit !== undefined) url.searchParams.set("limit", String(options.limit));
  if (options.tz) url.searchParams.set("tz", options.tz);
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "list activities", config.secrets);
  return (await response.json()) as OmnigentPaginatedList<OmnigentActivity>;
}

/** `GET /v1/sessions/{superId}/activities/{activityId}` — one Activity with full step detail. */
export async function getOmnigentActivity(
  config: OmnigentClientConfig,
  email: string,
  superSessionId: string,
  activityId: string,
  options: { tz?: string } = {},
): Promise<OmnigentActivity> {
  const url = new URL(
    `/v1/sessions/${encodeURIComponent(superSessionId)}/activities/${encodeURIComponent(activityId)}`,
    config.baseUrl,
  );
  if (options.tz) url.searchParams.set("tz", options.tz);
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "get activity", config.secrets);
  return (await response.json()) as OmnigentActivity;
}

/** `GET /v1/sessions/{superId}/activities/stream` — a frame on connect and one each time the
 * family's Activity Feed may have changed (a Helper started, stepped, settled). No content: the
 * caller re-reads the feed. Ends when the response ends or `signal` aborts. */
export function streamOmnigentActivityChanges(
  config: OmnigentClientConfig,
  email: string,
  superSessionId: string,
  signal?: AbortSignal,
) {
  return streamOmnigentSse(
    config,
    email,
    `/v1/sessions/${encodeURIComponent(superSessionId)}/activities/stream`,
    { signal, what: "watch activities" },
  );
}

function epochSecondsToIso(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString();
}

export function mapActivityStep(raw: OmnigentActivityStep): ActivityStep {
  return {
    itemId: raw.item_id,
    title: raw.title,
    createdAt: epochSecondsToIso(raw.created_at),
    tool: raw.tool ?? null,
    detail: raw.detail ?? undefined,
  };
}

/** What the engine says when a run failed and it has nothing more specific. */
const GENERIC_FAILURE = "Could not complete the task";

const FAILURE_REASONS: ReadonlyArray<readonly [RegExp, string]> = [
  [
    /selected model|may not exist or you may not have access|model .*not found/i,
    "The model isn't available.",
  ],
  [/overloaded|rate.?limit|too many requests|capacity|\b(?:429|503|529)\b/i, "The model was busy."],
  [/credit|billing|quota|insufficient/i, "The model account hit its limit."],
  [/time(?:d)?[ -]?out|deadline/i, "It took too long."],
  [/connect failed|econn|network|unreachable|no runner|host/i, "Couldn't get started."],
];

/**
 * A failed run's outcome is the engine's own error text, which can be a stack of internals
 * (paths, codes, env names). Shows a short plain reason when it can tell what happened, the
 * text itself when it is already short and plain, and the generic line otherwise.
 */
export function plainFailureOutcome(raw: string | null): string | null {
  const text = raw?.replace(/^inner executor error:\s*/i, "").trim();
  if (!text) return null;
  if (text === GENERIC_FAILURE) return text;
  const known = FAILURE_REASONS.find(([pattern]) => pattern.test(text));
  if (known) return known[1];
  const plain = text.length <= 100 && !/[\\/\n]/.test(text) && !isEngineErrorText(text);
  return plain ? text : GENERIC_FAILURE;
}

/** The kinds of tool the engine names for an Activity; one it does not know is dropped. */
function mapFamilies(raw: unknown): ToolFamily[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((value) => {
    const family = ToolFamilySchema.safeParse(value);
    return family.success ? [family.data] : [];
  });
}

export function mapActivity(raw: OmnigentActivity): Activity {
  const failed = raw.status === "failed";
  return {
    id: raw.id,
    kind: raw.kind,
    chatId: raw.chat_id,
    source: raw.source,
    title: raw.title,
    outcome: failed ? plainFailureOutcome(raw.outcome) : (raw.outcome ?? null),
    summary: failed ? plainFailureOutcome(raw.summary ?? raw.outcome) : (raw.summary ?? null),
    status: raw.status,
    startedAt: epochSecondsToIso(raw.started_at),
    finishedAt: raw.finished_at === null ? null : epochSecondsToIso(raw.finished_at),
    date: raw.date,
    parentChatId: raw.parent_chat_id ?? null,
    ...(raw.artifact
      ? {
          artifact: {
            groupId: raw.artifact.group_id,
            name: raw.artifact.name,
            ...(raw.artifact.round_id ? { roundId: raw.artifact.round_id } : {}),
          },
        }
      : {}),
    steps: raw.steps?.map(mapActivityStep),
    summaryLine: typeof raw.summary_line === "string" && raw.summary_line ? raw.summary_line : null,
    families: mapFamilies(raw.families),
    ...(raw.paused === true ? { paused: true } : {}),
    ...(raw.round_id ? { roundId: raw.round_id } : {}),
  };
}
