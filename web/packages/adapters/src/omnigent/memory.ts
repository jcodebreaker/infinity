import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";

export async function getOmnigentMemoryProfile(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<{ profile: string | null }> {
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/memory/profile`, config.baseUrl),
    { headers: omnigentHeaders(config, email) },
  );
  await throwOnError(response, "get memory profile", config.secrets);
  const body = (await response.json()) as { profile?: string | null };
  return { profile: body.profile || null };
}

/** A memory claim as the engine serves it (`memory/service.py` `_claim_to_dict`). */
export interface OmnigentMemoryClaim {
  claim_id: string;
  kind: string;
  text: string;
  origin: "said" | "edited" | "noticed";
  person_authored: boolean;
  last_confirmed: number;
  /** Derived by the engine: an active claim past its `valid_until` reads as `expired`. */
  status: "active" | "expired";
}

/** `GET .../memory/claims` — every active claim, newest first. */
export async function listOmnigentMemoryClaims(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<OmnigentMemoryClaim[]> {
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/memory/claims`, config.baseUrl),
    { headers: omnigentHeaders(config, email) },
  );
  await throwOnError(response, "list memory claims", config.secrets);
  const body = (await response.json()) as { claims?: OmnigentMemoryClaim[] };
  return body.claims ?? [];
}

/** `PATCH .../memory/claims/{id}` — the person's edit; the claim becomes person-authored. */
export async function patchOmnigentMemoryClaim(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  claimId: string,
  text: string,
): Promise<OmnigentMemoryClaim> {
  const response = await fetch(
    new URL(
      `/v1/sessions/${encodeURIComponent(sessionId)}/memory/claims/${encodeURIComponent(claimId)}`,
      config.baseUrl,
    ),
    { method: "PATCH", headers: omnigentHeaders(config, email), body: JSON.stringify({ text }) },
  );
  await throwOnError(response, "edit memory claim", config.secrets);
  return (await response.json()) as OmnigentMemoryClaim;
}

/** `POST .../memory/forget` with `confirm` — the person deletes one claim. */
export async function forgetOmnigentMemoryClaim(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  claimId: string,
): Promise<void> {
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/memory/forget`, config.baseUrl),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({ claim_id: claimId, confirm: true }),
    },
  );
  await throwOnError(response, "forget memory claim", config.secrets);
}
