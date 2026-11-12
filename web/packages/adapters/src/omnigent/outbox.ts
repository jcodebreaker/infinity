import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";

/** How many proactive messages the person has not seen (`superchat/outbox/routes.py`). */
export interface OmnigentOutboxUnread {
  count: number;
  first_item_id: string | null;
  last_seen_item_id: string | null;
}

/** `GET /v1/me/outbox/unread` — the proactive messages after the last one the person saw. */
export async function getOmnigentOutboxUnread(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentOutboxUnread> {
  const response = await fetch(new URL("/v1/me/outbox/unread", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "read unread messages", config.secrets);
  return (await response.json()) as OmnigentOutboxUnread;
}

/** `POST /v1/me/outbox/seen` — the person saw the Conversation up to `itemId` (never moves back). */
export async function markOmnigentOutboxSeen(
  config: OmnigentClientConfig,
  email: string,
  itemId: string,
): Promise<OmnigentOutboxUnread> {
  const response = await fetch(new URL("/v1/me/outbox/seen", config.baseUrl), {
    method: "POST",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify({ item_id: itemId }),
  });
  await throwOnError(response, "mark messages seen", config.secrets);
  return (await response.json()) as OmnigentOutboxUnread;
}

/** `POST /v1/me/outbox/{itemId}/not-useful` — the person's *Not useful* on a proactive message. */
export async function markOmnigentOutboxNotUseful(
  config: OmnigentClientConfig,
  email: string,
  itemId: string,
  reason?: string,
): Promise<void> {
  const response = await fetch(
    new URL(`/v1/me/outbox/${encodeURIComponent(itemId)}/not-useful`, config.baseUrl),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify(reason ? { reason } : {}),
    },
  );
  await throwOnError(response, "mark message not useful", config.secrets);
}
