import type { OmnigentClientConfig } from "./core.js";
import { omnigentHeaders, throwOnError } from "./core.js";
import { streamOmnigentSse } from "./sessions.js";

/** One block of a transcript message (engine/omnigent/superchat/transcript/blocks.py). */
export type OmnigentTranscriptBlock =
  | { type: "text"; text: string }
  | {
      type: "card";
      card_id?: string | null;
      card: {
        card: string;
        id?: string;
        title?: string;
        data: Record<string, unknown>;
        fallback: string;
      };
      pending?: boolean;
    }
  | { type: "helper"; call_id: string; session_id: string; title: string; status: string }
  | {
      type: "file";
      artifact_id: string;
      name: string;
      mime: string | null;
      kind?: string;
      size?: number;
      title?: string;
      version?: number;
      versions?: number;
      /** Set when the person's own action delivered the file (an export from the panel). */
      by?: "user";
    }
  | { type: "secure_entry"; request_id: string; name: string; site: string; reason?: string }
  | { type: "error"; code: string; level?: "info" }
  | {
      /** A studio runtime line (ADR 0024); only in a file's studio chat, as a `system` message. */
      type: "notice";
      kind: "hand_edit" | "version" | "stale" | "queued";
      version?: number;
      element_id?: string;
      element?: string;
      what?: string;
      origin?: "superchat" | "update";
    };

export interface OmnigentTranscriptMessage {
  id: string;
  /** `system`: a runtime note the engine shows only in a file's studio chat (ADR 0024), such as
   * a version saved elsewhere during a turn. */
  role: "user" | "assistant" | "system";
  created_at: number | null;
  blocks: OmnigentTranscriptBlock[];
  /** `queued` while this user message waits behind the running turn. */
  delivered?: "queued";
  /** An assistant message: the Activity of the turn that wrote it (ADR 0027). */
  activity_id?: string;
  /** A user message only: the artifact it is about (docs/conversation.md), or null for none. */
  artifact?: OmnigentMessageArtifact | null;
  /** An assistant message a scheduled run's turn wrote, not one the person asked for (ADR 0036). */
  proactive?: OmnigentProactive;
}

/** How a proactive message came: the source's kind (`automation`, `topic_watch`, `daily_brief`,
 * …), its topic when it named one, and how soon it matters. */
export interface OmnigentProactive {
  kind: string;
  topic_key?: string;
  urgency: "now" | "today" | "brief";
}

/** The artifact a person's message links to: `open` beside the chat when sent, `edited` by a
 * save during its turn, `chosen` on the chip. */
export interface OmnigentMessageArtifact {
  artifact_id: string;
  name: string;
  version: number | null;
  how: "open" | "edited" | "chosen";
}

export interface OmnigentTranscriptLineage {
  kind: "super" | "helper" | null;
  root_id: string | null;
  parent_id: string | null;
}

export interface OmnigentTranscriptPage {
  data: OmnigentTranscriptMessage[];
  has_more: boolean;
  older_cursor: string | null;
  lineage: OmnigentTranscriptLineage;
  /** The chat was cleared: the page starts at this checkpoint. */
  reset?: { item_id: string; created_at: number | null } | null;
  /** A turn is running in the chat right now. */
  live?: boolean;
}

/** `GET /v1/sessions/{id}/transcript` — a session's messages as typed blocks, oldest first.
 * `before` is a previous page's `older_cursor`. */
export async function getOmnigentTranscript(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  options: { limit?: number; before?: string; beforeReset?: boolean } = {},
): Promise<OmnigentTranscriptPage> {
  const url = new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/transcript`, config.baseUrl);
  if (options.limit !== undefined) url.searchParams.set("limit", String(options.limit));
  if (options.before) url.searchParams.set("before", options.before);
  if (options.beforeReset) url.searchParams.set("before_reset", "true");
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "get transcript", config.secrets);
  return (await response.json()) as OmnigentTranscriptPage;
}

/** `POST /v1/sessions/{id}/items/{itemId}/artifact` — the person picks which artifact their
 * message is about (`null`: none). The engine records it as `chosen`, steers a running turn and
 * answers with the updated transcript message. */
export async function setOmnigentMessageArtifact(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  itemId: string,
  artifactId: string | null,
): Promise<OmnigentTranscriptMessage> {
  const response = await fetch(
    new URL(
      `/v1/sessions/${encodeURIComponent(sessionId)}/items/${encodeURIComponent(itemId)}/artifact`,
      config.baseUrl,
    ),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({ artifact_id: artifactId }),
    },
  );
  await throwOnError(response, "set message artifact", config.secrets);
  return (await response.json()) as OmnigentTranscriptMessage;
}

/** A Round as the engine lists it (`RoundSummary`, engine/omnigent/server/schemas.py); times
 * are epoch seconds. */
export interface OmnigentRoundSummary {
  round_id: string;
  status: string;
  started_at: number | null;
  settled_at: number | null;
  delivered_at: number | null;
  live: string | null;
  helpers: number;
  files: number;
  steps: number;
}

/** Who acted in a Round: the Assistant (`helper_id` null) or a Helper. */
interface OmnigentRoundActor {
  actor: string | null;
  helper_id: string | null;
}

/** One Round folded from its events (`RoundView`); times are epoch seconds. Only the fields the
 * web reads are typed. */
export interface OmnigentRoundView {
  round_id: string;
  status: string;
  started_at: number | null;
  settled_at: number | null;
  elapsed: number;
  delivered_at?: number | null;
  stop_line: string | null;
  now: (OmnigentRoundActor & { name: string | null; digest: string | null }) | null;
  trail: Array<
    OmnigentRoundActor & { t: number; name: string | null; digest: string | null; ok: boolean }
  >;
  helpers: Record<
    string,
    {
      id: string;
      title: string | null;
      folder: string | null;
      status: string;
      started_at: number;
      finished_at: number | null;
    }
  >;
  files: Array<{
    name: string | null;
    artifact_id: string | null;
    version: number | null;
    helper_id: string | null;
  }>;
  checklist: Array<{
    id: string | null;
    subject: string | null;
    active_form: string | null;
    status: string;
  }>;
}

/** `GET /v1/sessions/{id}/rounds`: the Conversation's latest Rounds, newest first. */
export async function listOmnigentRounds(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  limit = 50,
): Promise<OmnigentRoundSummary[]> {
  const url = new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/rounds`, config.baseUrl);
  url.searchParams.set("limit", String(limit));
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "list rounds", config.secrets);
  return ((await response.json()) as { data: OmnigentRoundSummary[] }).data;
}

/** `GET /v1/sessions/{id}/rounds/{roundId}`: one Round of the Conversation, folded. */
export async function getOmnigentRound(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  roundId: string,
): Promise<OmnigentRoundView> {
  const response = await fetch(
    new URL(
      `/v1/sessions/${encodeURIComponent(sessionId)}/rounds/${encodeURIComponent(roundId)}`,
      config.baseUrl,
    ),
    { headers: omnigentHeaders(config, email) },
  );
  await throwOnError(response, "get round", config.secrets);
  return (await response.json()) as OmnigentRoundView;
}

/** A frame of the family stream. Ids only; the caller refetches what changed. */
export type OmnigentFamilyEvent =
  | { type: "message.done"; chat_id: string; item_id: string }
  | { type: "message.delivery"; chat_id: string; item_id: string }
  | { type: "turn.done"; chat_id: string; status: string }
  | { type: "chat.reset"; chat_id: string; item_id: string }
  | { type: "chats.changed"; root_id: string }
  | { type: "activities.changed"; root_id: string }
  | { type: "artifacts.changed"; root_id: string; group_id: string }
  | ({ type: "round.updated"; root_id: string } & OmnigentRoundSummary)
  | { type: "chat.status"; chat_id: string; status: string; blocked_on: string | null }
  | { type: "asks.changed"; root_id: string }
  | { type: "session.heartbeat" };

/** `GET /v1/sessions/{root}/family/stream` — one SSE stream for a Conversation and its
 * Helpers. Ends when the response ends or `signal` aborts. */
export async function* streamOmnigentFamily(
  config: OmnigentClientConfig,
  email: string,
  rootSessionId: string,
  signal?: AbortSignal,
): AsyncGenerator<OmnigentFamilyEvent> {
  for await (const frame of streamOmnigentSse(
    config,
    email,
    `/v1/sessions/${encodeURIComponent(rootSessionId)}/family/stream`,
    { signal, what: "watch family" },
  )) {
    if (
      frame.type === "message.done" ||
      frame.type === "message.delivery" ||
      frame.type === "turn.done" ||
      frame.type === "chat.reset" ||
      frame.type === "chats.changed" ||
      frame.type === "activities.changed" ||
      frame.type === "artifacts.changed" ||
      frame.type === "round.updated" ||
      frame.type === "chat.status" ||
      frame.type === "asks.changed" ||
      frame.type === "session.heartbeat"
    ) {
      yield frame as unknown as OmnigentFamilyEvent;
    }
  }
}

/** A message the engine's family search found: `sessionId` is the chat it is in (the
 * Conversation, or another chat of its family), `id` its transcript message's id. */
export interface OmnigentSearchHit {
  sessionId: string;
  id: string;
  role: "user" | "assistant";
  text: string;
}

/** `GET /v1/sessions/{root}/items/search?scope=family` — full-text search over the
 * Conversation's family, ranked. Only the person's and the Muse's messages are
 * returned (tool calls and outputs are the engine's plumbing); the engine has already
 * redacted the text. */
export async function searchOmnigentFamily(
  config: OmnigentClientConfig,
  email: string,
  rootSessionId: string,
  query: string,
  limit = 10,
): Promise<OmnigentSearchHit[]> {
  const url = new URL(
    `/v1/sessions/${encodeURIComponent(rootSessionId)}/items/search`,
    config.baseUrl,
  );
  url.searchParams.set("query", query);
  url.searchParams.set("scope", "family");
  url.searchParams.set("limit", String(Math.min(limit, 20)));
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "search family", config.secrets);
  const body = (await response.json()) as {
    data?: Array<{
      session_id?: unknown;
      message_id?: unknown;
      role?: unknown;
      text?: unknown;
    }>;
  };
  const hits: OmnigentSearchHit[] = [];
  for (const hit of body.data ?? []) {
    if (hit.role !== "user" && hit.role !== "assistant") continue;
    if (typeof hit.session_id !== "string" || typeof hit.message_id !== "string") continue;
    const text = typeof hit.text === "string" ? hit.text.trim() : "";
    if (text) hits.push({ sessionId: hit.session_id, id: hit.message_id, role: hit.role, text });
  }
  return hits;
}
