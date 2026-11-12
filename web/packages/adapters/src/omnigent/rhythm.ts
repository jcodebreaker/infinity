import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";

/** `POST /v1/me/rhythm/events` — what the person did with a proactive message (ADR 0038). */
export async function reportOmnigentRhythmEvent(
  config: OmnigentClientConfig,
  email: string,
  itemId: string,
  event: "shown" | "opened" | "acted_on",
): Promise<void> {
  const response = await fetch(new URL("/v1/me/rhythm/events", config.baseUrl), {
    method: "POST",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify({ item_id: itemId, event }),
  });
  await throwOnError(response, "report a message event", config.secrets);
}
