import type { OmnigentClientConfig, OmnigentSessionResponse, OmnigentStreamEvent } from "./core.js";
import { omnigentHeaders, throwOnError } from "./core.js";

/** Where a session sits in a Super Chat family (ADR 0009), or `null` outside one. */
export interface OmnigentSessionSuperchat {
  kind: "super" | "helper" | null;
  /** The family's Super Chat. */
  root_id: string | null;
  /** A Helper's direct parent. */
  parent_id: string | null;
  /** The Project the session has open (ADR 0008). */
  project: { slug: string; name: string } | null;
  /** The working directory relative to the workspace (`null` at its root). */
  folder?: string | null;
  /** The person chose that folder themselves; the Muse keeps it. */
  workspace_chosen?: boolean;
}

/** `GET /v1/sessions/{id}` — a cheap session snapshot (no items, liveness or usage). */
export interface OmnigentSessionSnapshot extends OmnigentSessionResponse {
  host_id?: string | null;
  labels?: Record<string, string>;
  superchat?: OmnigentSessionSuperchat | null;
}

export async function getOmnigentSession(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<OmnigentSessionSnapshot> {
  const url = new URL(`/v1/sessions/${encodeURIComponent(sessionId)}`, config.baseUrl);
  url.searchParams.set("include_items", "false");
  url.searchParams.set("include_liveness", "false");
  url.searchParams.set("include_usage", "false");
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "get session", config.secrets);
  return (await response.json()) as OmnigentSessionSnapshot;
}

/** `PUT /v1/sessions/{id}/workspace` — moves the session's working directory to `workspace`, an
 * absolute path on its host; the engine refuses one outside the agent's workspace boundary. */
export async function setOmnigentSessionWorkspace(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  workspace: string,
): Promise<string> {
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/workspace`, config.baseUrl),
    {
      method: "PUT",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({ workspace }),
    },
  );
  await throwOnError(response, "set session workspace", config.secrets);
  const body = (await response.json()) as { workspace: string };
  return body.workspace;
}

/** `POST /v1/sessions/{id}/events` `interrupt`: stops the running turn and its running Helpers;
 * messages waiting behind it are cancelled (`session.input.delivery` `cancelled`). `delivered`:
 * whether the stop landed (false when no runner took it and nothing was waiting). */
export async function interruptOmnigentSession(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<{ delivered: boolean }> {
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/events`, config.baseUrl),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({ type: "interrupt", data: {} }),
    },
  );
  await throwOnError(response, "interrupt session", config.secrets);
  const body = (await response.json().catch(() => ({}))) as { delivered?: unknown };
  return { delivered: body.delivered === true };
}

/** `POST /v1/sessions/{id}/helpers/pause|resume` (ADR 0027): holds a Conversation's Helpers at
 * their next tool boundary, or lets them go on. `helperIds` narrows it (default: all of them);
 * answers with the Helpers it acted on. */
export async function setOmnigentHelpersPaused(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  paused: boolean,
  helperIds?: string[],
): Promise<string[]> {
  const action = paused ? "pause" : "resume";
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/helpers/${action}`, config.baseUrl),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify(helperIds ? { helper_ids: helperIds } : {}),
    },
  );
  await throwOnError(response, `${action} helpers`, config.secrets);
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  const ids = body[paused ? "paused" : "resumed"];
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [];
}

/** What the engine answers for a posted message: its stored id, and the runner's number for the
 * turn it started or joined (`session.status` edges carry the same number). */
export interface OmnigentPostedMessage {
  itemId: string | undefined;
  turn: number | undefined;
}

/** What is beside the chat when a message is sent (docs/conversation.md "Send"). */
export interface OmnigentMessageContext {
  openArtifact?: { artifactId: string; version: number };
  /** The ask card option the message answers (ADR 0025). */
  cardAnswer?: { cardId: string; optionId: string; label: string };
}

/** `POST /v1/sessions/{id}/events` with a `message` event carrying the user's turn text; the
 * engine reads the images its reference lines name. `context` rides beside the message data
 * (never in the model's input item): the artifact open beside the chat, for the engine's runtime
 * hint and the message's `open` link. */
export async function postOmnigentMessage(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  text: string,
  context?: OmnigentMessageContext,
): Promise<OmnigentPostedMessage> {
  const content = [{ type: "input_text", text }];
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/events`, config.baseUrl),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({
        type: "message",
        data: { role: "user", content },
        ...(context?.openArtifact || context?.cardAnswer
          ? {
              context: {
                ...(context.openArtifact
                  ? {
                      open_artifact: {
                        artifact_id: context.openArtifact.artifactId,
                        version: context.openArtifact.version,
                      },
                    }
                  : {}),
                ...(context.cardAnswer
                  ? {
                      card_answer: {
                        card_id: context.cardAnswer.cardId,
                        option_id: context.cardAnswer.optionId,
                        label: context.cardAnswer.label,
                      },
                    }
                  : {}),
              },
            }
          : {}),
      }),
    },
  );
  await throwOnError(response, "post message event", config.secrets);
  const body = (await response.json().catch(() => undefined)) as
    | { item_id?: unknown; turn?: unknown }
    | undefined;
  return {
    itemId: typeof body?.item_id === "string" ? body.item_id : undefined,
    turn: typeof body?.turn === "number" ? body.turn : undefined,
  };
}

/** `POST /v1/sessions/{id}/read` — the person has the chat open: moves their read baseline
 * (up to `itemId`, default now) and clears its unread flag. */
export async function markOmnigentRead(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  itemId?: string,
): Promise<void> {
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/read`, config.baseUrl),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify(itemId ? { item_id: itemId } : {}),
    },
  );
  await throwOnError(response, "mark read", config.secrets);
}

/** `POST /v1/sessions/{id}/reset` — clears a Super Chat's Conversation. Owner only; the
 * engine answers 409 (`conflict`) while a turn is running. */
export async function resetOmnigentSession(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<{ session_id: string; reset_item_id: string | null; created_at: number | null }> {
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/reset`, config.baseUrl),
    { method: "POST", headers: omnigentHeaders(config, email) },
  );
  await throwOnError(response, "reset session", config.secrets);
  return (await response.json()) as {
    session_id: string;
    reset_item_id: string | null;
    created_at: number | null;
  };
}

export interface OmnigentPaginatedList<T> {
  data: T[];
  first_id?: string | null;
  last_id?: string | null;
  has_more?: boolean;
}

/**
 * `GET /v1/sessions/{id}/stream` — live SSE tail. Yields each parsed `data:` frame (skipping
 * the `[DONE]` sentinel and unparsable keepalive lines) until the response body ends or
 * `signal` aborts.
 */
export function streamOmnigentSession(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  signal?: AbortSignal,
): AsyncGenerator<OmnigentStreamEvent> {
  return streamOmnigentSse(config, email, `/v1/sessions/${encodeURIComponent(sessionId)}/stream`, {
    signal,
    what: "stream session",
  });
}

/** Any Omnigent SSE endpoint: yields each parsed `data:` frame (see `streamOmnigentSession`). */
export async function* streamOmnigentSse(
  config: OmnigentClientConfig,
  email: string,
  path: string,
  options: { signal?: AbortSignal; what: string },
): AsyncGenerator<OmnigentStreamEvent> {
  const response = await fetch(new URL(path, config.baseUrl), {
    headers: { ...omnigentHeaders(config, email), accept: "text/event-stream" },
    signal: options.signal,
  });
  await throwOnError(response, options.what, config.secrets);
  if (!response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const data = chunk
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("");
        if (!data || data === "[DONE]") continue;
        try {
          const parsed = JSON.parse(data) as OmnigentStreamEvent;
          if (parsed?.type) yield parsed;
        } catch {
          // Ignore keepalives and partial frames, same as the mobile client.
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
