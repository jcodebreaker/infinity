import type { OmnigentClientConfig } from "./core.js";
import { omnigentHeaders, throwOnError } from "./core.js";

export type OmnigentProactivityLevel = "off" | "low" | "normal" | "high";

/** The person's proactivity level and quiet hours (local `HH:MM`, both or neither), as the
 * engine gates every proactive run with them. */
export interface OmnigentProactivity {
  proactivity: OmnigentProactivityLevel;
  quiet_start: string | null;
  quiet_end: string | null;
  timezone: string;
}

/** `GET /v1/me/proactivity` — the engine's defaults when the person never set any. */
export async function getOmnigentProactivity(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentProactivity> {
  const response = await fetch(new URL("/v1/me/proactivity", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "get proactivity", config.secrets);
  return (await response.json()) as OmnigentProactivity;
}

/** `PUT /v1/me/proactivity` — the engine keeps omitted fields, so sending only `timezone`
 * leaves the level and quiet hours alone; `null` quiet hours turn them off. */
export async function putOmnigentProactivity(
  config: OmnigentClientConfig,
  email: string,
  prefs: Partial<
    Pick<OmnigentProactivity, "proactivity" | "quiet_start" | "quiet_end" | "timezone">
  >,
): Promise<OmnigentProactivity> {
  const response = await fetch(new URL("/v1/me/proactivity", config.baseUrl), {
    method: "PUT",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify(prefs),
  });
  await throwOnError(response, "set proactivity", config.secrets);
  return (await response.json()) as OmnigentProactivity;
}

/** `PUT /v1/me/proactivity` with only the timezone. */
export async function putOmnigentTimezone(
  config: OmnigentClientConfig,
  email: string,
  timezone: string,
): Promise<void> {
  await putOmnigentProactivity(config, email, { timezone });
}
