import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";

/** A topic as the engine serves it (`superchat/topics/routes.py` `topic_to_response`). */
export interface OmnigentTopic {
  key: string;
  title: string;
  left_off: string;
  covered: { date: string; kind: string; text: string; file?: string }[];
  open: {
    id?: string;
    kind: string;
    text: string;
    due?: string;
    starter?: string;
    verb?: string;
  }[];
  starters: string[];
  events: { at: number; line: string }[];
  sources: { chats?: number; files?: number; emails?: number };
  status: string;
  since: string | null;
  since_at: number | null;
  tag?: string | null;
  status_line?: string | null;
  /** The Lookout's draft on the topic, while its message is unseen. */
  prepared?: {
    item_id: string;
    artifact_id?: string;
    title?: string;
    line?: string;
    at?: number;
  } | null;
  /** When the person last worked on it (a pick, or the Conversation's write), epoch seconds. */
  worked_at?: number | null;
  last_touched_at: number;
}

/** `GET /v1/me/topics?days=` — the person's open topics touched within `days`, newest first. */
export async function listOmnigentTopics(
  config: OmnigentClientConfig,
  email: string,
  days = 7,
): Promise<OmnigentTopic[]> {
  const url = new URL("/v1/me/topics", config.baseUrl);
  url.searchParams.set("days", String(days));
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "list topics", config.secrets);
  const body = (await response.json()) as { topics?: OmnigentTopic[] };
  return body.topics ?? [];
}

/** `POST /v1/me/topics/{key}/pick` — add the topic's card to the Conversation (no turn starts).
 * `itemId`: the proactive message the pick came from, which the rhythm counts as acted on. */
export async function pickOmnigentTopic(
  config: OmnigentClientConfig,
  email: string,
  key: string,
  conversationId: string,
  itemId?: string,
): Promise<void> {
  const response = await fetch(
    new URL(`/v1/me/topics/${encodeURIComponent(key)}/pick`, config.baseUrl),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({
        conversation_id: conversationId,
        ...(itemId ? { item_id: itemId } : {}),
      }),
    },
  );
  await throwOnError(response, "pick up topic", config.secrets);
}
