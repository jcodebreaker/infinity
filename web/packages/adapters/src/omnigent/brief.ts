import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";

/** The newest brief page (`superchat/brief/service.py` `latest`). */
export interface OmnigentBriefPage {
  artifact_id: string;
  group_id: string;
  title: string | null;
  version: number;
  created_at: number;
  /** The first thing that needs the person, or "Nothing needs you today"; `null` if unknown. */
  line: string | null;
  /** Home's lines, one per topic (absent from older engines). */
  home?: { topic_key: string; topic: string; text: string; bold: string }[];
}

/** `GET /v1/me/brief/latest`: the brief's task and its newest page. */
export interface OmnigentLatestBrief {
  /** Run it now with `POST /v1/scheduled-tasks/{task_id}/run`. `null` without a Conversation. */
  task_id: string | null;
  enabled: boolean;
  next_run_at: string | null;
  brief: OmnigentBriefPage | null;
}

/** `GET /v1/me/brief/latest`: the first call also sets the brief up for the person. */
export async function getOmnigentLatestBrief(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentLatestBrief> {
  const response = await fetch(new URL("/v1/me/brief/latest", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "read the latest brief", config.secrets);
  return (await response.json()) as OmnigentLatestBrief;
}
