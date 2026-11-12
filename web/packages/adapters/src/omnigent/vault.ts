import type { OmnigentClientConfig } from "./client/core.js";
import {
  errorCodeFromBody,
  OmnigentApiError,
  omnigentHeaders,
  throwOnError,
} from "./client/core.js";

/** A saved login as the engine lists it (`routes/vault.py` `entry_to_response`): never a value. */
export interface OmnigentVaultEntry {
  id: string;
  name: string;
  site: string;
  username: string;
  created_at: number;
  last_used_at: number | null;
}

/** A secure-entry request (`request_to_response`). */
export interface OmnigentVaultRequest {
  id: string;
  session_id: string;
  name: string;
  site: string;
  reason: string;
  status: "pending" | "saved" | "expired";
}

/** `GET /v1/me/vault` — the person's saved logins, metadata only. */
export async function listOmnigentVault(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentVaultEntry[]> {
  const response = await fetch(new URL("/v1/me/vault", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "list vault", config.secrets);
  return ((await response.json()) as { entries?: OmnigentVaultEntry[] }).entries ?? [];
}

/** `DELETE /v1/me/vault/{id}`. */
export async function deleteOmnigentVaultEntry(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<void> {
  const response = await fetch(new URL(`/v1/me/vault/${encodeURIComponent(id)}`, config.baseUrl), {
    method: "DELETE",
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "delete vault entry", config.secrets);
}

/** `GET /v1/me/vault/requests/{id}`. */
export async function getOmnigentVaultRequest(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<OmnigentVaultRequest> {
  const response = await fetch(
    new URL(`/v1/me/vault/requests/${encodeURIComponent(id)}`, config.baseUrl),
    { headers: omnigentHeaders(config, email) },
  );
  await throwOnError(response, "get vault request", config.secrets);
  return (await response.json()) as OmnigentVaultRequest;
}

/** `POST /v1/me/vault` — the value goes in once and is never echoed. */
export async function saveOmnigentVaultEntry(
  config: OmnigentClientConfig,
  email: string,
  entry: { name: string; site: string; username?: string; password: string; requestId?: string },
): Promise<OmnigentVaultEntry> {
  const response = await fetch(new URL("/v1/me/vault", config.baseUrl), {
    method: "POST",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify({
      name: entry.name,
      site: entry.site,
      username: entry.username ?? "",
      password: entry.password,
      ...(entry.requestId ? { request_id: entry.requestId } : {}),
    }),
  });
  if (!response.ok) {
    // A validation error body can echo the submitted input, so none of it is kept.
    const code = errorCodeFromBody(await response.text().catch(() => ""));
    throw new OmnigentApiError(`omnigent save vault entry failed (${response.status})`, code);
  }
  return (await response.json()) as OmnigentVaultEntry;
}
