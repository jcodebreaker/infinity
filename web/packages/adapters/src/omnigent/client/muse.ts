import type { OmnigentClientConfig } from "./core.js";
import { omnigentHeaders, throwOnError } from "./core.js";

/** The caller's Muse for the tenant (ADR 0009): its session, and the agent it runs on. */
export interface OmnigentMuse {
  session_id: string;
  agent: string;
  /** `true` when this call created it. */
  created: boolean;
}

/** `GET /v1/me/muse` — finds the caller's Muse for the tenant, creating it when there is none. */
export async function getOmnigentMuse(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentMuse> {
  const response = await fetch(new URL("/v1/me/muse", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "get muse", config.secrets);
  return (await response.json()) as OmnigentMuse;
}

/** `PUT /v1/me/muse` — runs the caller's Muse on the built-in `agent`. */
export async function putOmnigentMuseAgent(
  config: OmnigentClientConfig,
  email: string,
  agent: string,
): Promise<OmnigentMuse> {
  const response = await fetch(new URL("/v1/me/muse", config.baseUrl), {
    method: "PUT",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify({ agent }),
  });
  await throwOnError(response, "set muse agent", config.secrets);
  return (await response.json()) as OmnigentMuse;
}

/** `POST /v1/me/muse/adopt` — claims an existing Super Chat the caller owns as their Muse.
 * Fails with `not_found`, `not_a_super_chat`, `muse_already_set` or `muse_tenant_mismatch`. */
export async function adoptOmnigentMuse(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<OmnigentMuse> {
  const response = await fetch(new URL("/v1/me/muse/adopt", config.baseUrl), {
    method: "POST",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify({ session_id: sessionId }),
  });
  await throwOnError(response, "adopt muse", config.secrets);
  return (await response.json()) as OmnigentMuse;
}

/** Who the engine says the caller is (`GET /v1/me`). */
export interface OmnigentIdentity {
  userId: string;
  isAdmin: boolean;
}

/** `GET /v1/me` — the caller's identity, or `null` when the engine has none for the request (a
 * 401: not signed in). */
export async function getOmnigentIdentity(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentIdentity | null> {
  const response = await fetch(new URL("/v1/me", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  if (response.status === 401) return null;
  await throwOnError(response, "get me", config.secrets);
  const body = (await response.json()) as { user_id: string | null; is_admin?: boolean };
  if (!body.user_id) return null;
  return { userId: body.user_id, isAdmin: body.is_admin === true };
}
