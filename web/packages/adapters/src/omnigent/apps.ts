// Apps on the Omnigent side: a saved HTML artifact published at its own address
// (engine/omnigent/superchat/apps/routes.py). The publication shape rides on the
// artifact rows, so its types stay with artifacts.
import type { OmnigentPublication, OmnigentPublishAudience } from "./artifacts.js";
import { type OmnigentClientConfig, omnigentHeaders, throwOnError } from "./client.js";

function artifactUrl(config: OmnigentClientConfig, id: string, suffix = ""): URL {
  return new URL(`/v1/artifacts/${encodeURIComponent(id)}${suffix}`, config.baseUrl);
}

/** `POST /v1/artifacts/{id}/publish` — publish or republish (keeps the address). */
export async function publishOmnigentArtifact(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  body: { audience: OmnigentPublishAudience; version?: number },
): Promise<OmnigentPublication> {
  const response = await fetch(artifactUrl(config, id, "/publish"), {
    method: "POST",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify(body),
  });
  await throwOnError(response, "publish artifact", config.secrets);
  return (await response.json()) as OmnigentPublication;
}

/** `DELETE /v1/artifacts/{id}/publish` — take the app offline. */
export async function unpublishOmnigentArtifact(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<void> {
  const response = await fetch(artifactUrl(config, id, "/publish"), {
    method: "DELETE",
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "unpublish artifact", config.secrets);
}

/** `GET /v1/artifacts/{id}/publish` — the publish state, or null when not published. */
export async function getOmnigentPublication(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<OmnigentPublication | null> {
  const response = await fetch(artifactUrl(config, id, "/publish"), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "get publish state", config.secrets);
  const body = (await response.json()) as { publish: OmnigentPublication | null };
  return body.publish;
}

/**
 * The identity the engine accepts for `/v1/published/*` (its `OMNIGENT_PUBLISHED_GATEWAY_USER`,
 * routes.py `PUBLISHED_GATEWAY_USER_DEFAULT`). Only the `/apps/<slug>` gateway uses it, after it
 * has checked the viewer against the app's audience.
 */
export function omnigentPublishedGatewayEmail(env: NodeJS.ProcessEnv = process.env): string {
  return env.OMNIGENT_PUBLISHED_GATEWAY_USER?.trim() || "superchat-apps-gateway@superchat.invalid";
}

export interface OmnigentPublishedApp {
  html: Uint8Array;
  audience: OmnigentPublishAudience;
  /** The owner's engine identity (their email). */
  owner: string;
  version: number;
  title: string;
}

/** `GET /v1/published/{slug}` — the pinned HTML with its audience and owner; null when unknown. */
export async function fetchOmnigentPublishedApp(
  config: OmnigentClientConfig,
  slug: string,
): Promise<OmnigentPublishedApp | null> {
  const url = new URL(`/v1/published/${encodeURIComponent(slug)}`, config.baseUrl);
  const response = await fetch(url, {
    headers: omnigentHeaders(config, omnigentPublishedGatewayEmail()),
  });
  if (response.status === 404) return null;
  await throwOnError(response, "read published app", config.secrets);
  const audience = response.headers.get("x-published-audience");
  if (audience !== "owner" && audience !== "org" && audience !== "link") return null;
  return {
    html: new Uint8Array(await response.arrayBuffer()),
    audience,
    owner: decodeURIComponent(response.headers.get("x-published-owner") ?? ""),
    version: Number(response.headers.get("x-published-version") ?? 1),
    title: decodeURIComponent(response.headers.get("x-published-title") ?? ""),
  };
}

/** `POST /v1/published/{slug}/view` — count one open by an opaque viewer key. */
export async function recordOmnigentPublishedView(
  config: OmnigentClientConfig,
  slug: string,
  viewerKey: string,
): Promise<void> {
  const url = new URL(`/v1/published/${encodeURIComponent(slug)}/view`, config.baseUrl);
  const response = await fetch(url, {
    method: "POST",
    headers: omnigentHeaders(config, omnigentPublishedGatewayEmail()),
    body: JSON.stringify({ viewer_key: viewerKey }),
  });
  await throwOnError(response, "record published view", config.secrets);
}
