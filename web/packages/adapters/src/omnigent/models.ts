// The engine's model layer (engine/omnigent/superchat/models/*): a person's provider
// keys, the models their connection serves, their Foundation, Builder and Light per harness and
// their budget.
// Shapes are the engine's.
import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";

export type OmnigentModelScope = "user" | "org";

export interface OmnigentModelConnection {
  provider: string;
  hint: string;
  status: string;
  validated_at: number | null;
  label: string | null;
  scope: OmnigentModelScope;
}

export type OmnigentModelCategory = "foundation" | "builder" | "light";

export interface OmnigentModelRow {
  id: string;
  label: string;
  family?: string | null;
  is_default: boolean;
  categories: OmnigentModelCategory[];
  [key: string]: unknown;
}

export interface OmnigentModelCatalog {
  harness: string;
  status: string;
  provider_label: string | null;
  default_model: string | null;
  foundation_model: string | null;
  builder_model: string | null;
  light_model: string | null;
  picks: Record<OmnigentModelCategory, string | null>;
  automatic: Record<OmnigentModelCategory, string | null>;
  models: OmnigentModelRow[];
  error?: string;
}

export interface OmnigentBudget {
  monthly_limit_usd: number | null;
  at_limit: "stop" | "ask";
  spent_month_usd: number;
  period_start?: string;
  org_limit_usd?: number | null;
  org_at_limit?: "stop" | "ask" | null;
  org_spent_month_usd?: number;
}

/** An upstream the engine has configured: what a key can be for. */
export interface OmnigentModelProvider {
  provider: string;
  label: string;
  operator_key: boolean;
}

async function call<T>(
  config: OmnigentClientConfig,
  email: string,
  what: string,
  path: string,
  init: { method?: string; body?: unknown } = {},
): Promise<T> {
  const response = await fetch(new URL(path, config.baseUrl), {
    method: init.method ?? "GET",
    headers: omnigentHeaders(config, email),
    ...(init.body !== undefined ? { body: JSON.stringify(init.body) } : {}),
  });
  await throwOnError(response, what, config.secrets);
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

const seg = encodeURIComponent;

const CONNECTIONS = "/v1/me/model-connections";

/** The person's own provider keys. */
export async function listOmnigentModelConnections(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentModelConnection[]> {
  return (
    await call<{ data: OmnigentModelConnection[] }>(
      config,
      email,
      "list model connections",
      CONNECTIONS,
    )
  ).data;
}

/** The engine probes the key with the provider before sealing it. */
export async function putOmnigentModelConnection(
  config: OmnigentClientConfig,
  email: string,
  provider: string,
  body: { api_key: string; label?: string },
): Promise<OmnigentModelConnection> {
  return call(config, email, "save model connection", `${CONNECTIONS}/${seg(provider)}`, {
    method: "PUT",
    body,
  });
}

export async function deleteOmnigentModelConnection(
  config: OmnigentClientConfig,
  email: string,
  provider: string,
): Promise<void> {
  await call(config, email, "remove model connection", `${CONNECTIONS}/${seg(provider)}`, {
    method: "DELETE",
  });
}

/** `GET /v1/me/model-providers`: the engine's configured upstreams, in the order it lists them. */
export async function listOmnigentModelProviders(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentModelProvider[]> {
  return (
    await call<{ data: OmnigentModelProvider[] }>(
      config,
      email,
      "list model providers",
      "/v1/me/model-providers",
    )
  ).data;
}

export async function listOmnigentModels(
  config: OmnigentClientConfig,
  email: string,
  harness: string,
): Promise<OmnigentModelCatalog> {
  return call(config, email, "list models", `/v1/me/models?harness=${seg(harness)}`);
}

/** The person's own picks per harness, `{harness: model}` per category. */
export type OmnigentModelPicks = Record<OmnigentModelCategory, Record<string, string>>;

function picksOf(raw: Partial<OmnigentModelPicks>): OmnigentModelPicks {
  return { foundation: raw.foundation ?? {}, builder: raw.builder ?? {}, light: raw.light ?? {} };
}

export async function getOmnigentModelPicks(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentModelPicks> {
  return picksOf(
    await call<Partial<OmnigentModelPicks>>(
      config,
      email,
      "get model preferences",
      "/v1/me/model-preferences",
    ),
  );
}

/** Replaces the category maps given; the engine keeps a map left out. */
export async function putOmnigentModelPicks(
  config: OmnigentClientConfig,
  email: string,
  picks: Partial<OmnigentModelPicks>,
): Promise<OmnigentModelPicks> {
  return picksOf(
    await call<Partial<OmnigentModelPicks>>(
      config,
      email,
      "set model preferences",
      "/v1/me/model-preferences",
      { method: "PUT", body: picks },
    ),
  );
}

/** The person's own budget (`/v1/me/budget`), with the organization's cap read-only. */
export async function getOmnigentBudget(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentBudget> {
  return call(config, email, "get budget", "/v1/me/budget");
}

export async function putOmnigentBudget(
  config: OmnigentClientConfig,
  email: string,
  budget: { monthly_limit_usd: number | null; at_limit: "stop" | "ask" },
): Promise<OmnigentBudget> {
  return call(config, email, "set budget", "/v1/me/budget", {
    method: "PUT",
    body: budget,
  });
}

/** The session's stored model (`model`, `null`: none) and the one the engine runs its next turn
 * on (`effectiveModel`: the stored one, else a Super Chat's Foundation pick). */
export async function getOmnigentSessionModel(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<{ model: string | null; effectiveModel: string | null }> {
  const session = await call<{ model_override?: string | null; effective_model?: string | null }>(
    config,
    email,
    "get session model",
    `/v1/sessions/${seg(sessionId)}?include_items=false&include_liveness=false&include_usage=false`,
  );
  return { model: session.model_override ?? null, effectiveModel: session.effective_model ?? null };
}

/** `PATCH /v1/sessions/{id}` with `model_override`; `null` ("default") makes the session follow
 * the person's Foundation pick. */
export async function setOmnigentSessionModel(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  model: string | null,
): Promise<void> {
  await call(config, email, "set session model", `/v1/sessions/${seg(sessionId)}`, {
    method: "PATCH",
    body: { model_override: model ?? "default" },
  });
}

// Account safety for signups (quarantine and a clean approval): the engine's account pause and
// reset, called as the deployment owner and refused by the engine unless it counts them an admin.

export async function setOmnigentUserSuspended(
  config: OmnigentClientConfig,
  email: string,
  userId: string,
  suspended: boolean,
): Promise<void> {
  await call(
    config,
    email,
    suspended ? "suspend user" : "resume user",
    `/v1/admin/users/${seg(userId)}/${suspended ? "suspend" : "resume"}`,
    { method: "POST" },
  );
}

export async function deleteOmnigentUser(
  config: OmnigentClientConfig,
  email: string,
  userId: string,
): Promise<void> {
  await call(config, email, "delete user", `/v1/admin/users/${seg(userId)}`, {
    method: "DELETE",
  });
}
