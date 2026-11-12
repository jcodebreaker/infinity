// Live artifacts on the Omnigent side: a page kept up to date on a schedule
// (engine/omnigent/superchat/live/routes.py). A page is addressed by the id of any of
// its versions.
import { type OmnigentClientConfig, omnigentHeaders, throwOnError } from "./client.js";

export type OmnigentLiveTell = "notable" | "every" | "never";

/** One part of the page an update changed (`plan_changes`). */
export interface OmnigentLiveChange {
  id: string;
  before: string;
  after: string;
  source: string | null;
  notable: boolean;
}

/** A part the person changed, with what the source says now. */
export interface OmnigentLiveSuggestion {
  id: string;
  yours: string;
  source_value: string;
  source: string | null;
}

/** Why an update was not published: it has suggestions, changes it could not place, or it sits on
 * hand edits the person has not published. */
export type OmnigentLiveHeldReason = "suggestions" | "unplaced" | "edits";

export interface OmnigentLive {
  task_id: string;
  rrule: string | null;
  timezone: string | null;
  /** The scheduled task's state: `active`, `paused` or `missing`. */
  schedule: string;
  last_run_at: number | null;
  sources: string[];
  tell: OmnigentLiveTell;
  follow_publish: boolean;
  on_change: boolean;
  note: string;
  /** An update the published copy held back until the person publishes it. */
  held: { version: number; reason: OmnigentLiveHeldReason } | null;
  checked_at: number | null;
  open_issue: string | null;
  last_update: {
    version: number | null;
    applied: OmnigentLiveChange[];
    suggestions: OmnigentLiveSuggestion[];
    at: number;
  } | null;
}

export interface OmnigentLiveSettings {
  rrule: string;
  sources?: string[];
  tell?: OmnigentLiveTell;
  follow_publish?: boolean;
  on_change?: boolean;
  note?: string;
}

function liveUrl(config: OmnigentClientConfig, id: string, suffix = ""): URL {
  return new URL(`/v1/artifacts/${encodeURIComponent(id)}/live${suffix}`, config.baseUrl);
}

async function send<T>(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  method: string,
  suffix: string,
  what: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch(liveUrl(config, id, suffix), {
    method,
    headers: omnigentHeaders(config, email),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  await throwOnError(response, what, config.secrets);
  return (await response.json()) as T;
}

/** `GET /v1/artifacts/{id}/live` — the settings and last update, or null when not live. */
export async function getOmnigentLive(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<OmnigentLive | null> {
  const body = await send<{ live: OmnigentLive | null }>(
    config,
    email,
    id,
    "GET",
    "",
    "get live settings",
  );
  return body.live;
}

/** `PUT /v1/artifacts/{id}/live` — keep the page updated (create or change). */
export async function putOmnigentLive(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  settings: OmnigentLiveSettings,
): Promise<OmnigentLive | null> {
  const body = await send<{ live: OmnigentLive | null }>(
    config,
    email,
    id,
    "PUT",
    "",
    "save live settings",
    settings,
  );
  return body.live;
}

/** `DELETE /v1/artifacts/{id}/live` — stop updating; the versions stay. */
export async function deleteOmnigentLive(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<void> {
  await send(config, email, id, "DELETE", "", "stop live updates");
}

/** `POST /v1/artifacts/{id}/live/reset` — start fresh; the settings stay. */
export async function resetOmnigentLive(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<OmnigentLive | null> {
  const body = await send<{ live: OmnigentLive | null }>(
    config,
    email,
    id,
    "POST",
    "/reset",
    "start live page fresh",
  );
  return body.live;
}

/** `POST /v1/artifacts/{id}/live/publish` — publish the update the published copy held back. */
export async function publishOmnigentLive(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<OmnigentLive | null> {
  const body = await send<{ live: OmnigentLive | null }>(
    config,
    email,
    id,
    "POST",
    "/publish",
    "publish the held update",
  );
  return body.live;
}

/** `POST /v1/artifacts/{id}/live/run` — update now. */
export async function runOmnigentLive(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<void> {
  await send(config, email, id, "POST", "/run", "run live update");
}

/** `POST /v1/artifacts/{id}/live/undo` — undo the newest update; returns the new version. */
export async function undoOmnigentLive(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<{ id: string; version: number }> {
  return send(config, email, id, "POST", "/undo", "undo live update");
}

/** `POST /v1/artifacts/{id}/live/accept` — take the source's value for one part. */
export async function acceptOmnigentLive(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  elementId: string,
): Promise<{ id: string; version: number }> {
  return send(config, email, id, "POST", "/accept", "accept source value", {
    element_id: elementId,
  });
}

/** One live page in `GET /v1/me/live-pages`; `artifact_id` is its newest version. Times are epoch
 * seconds. */
export interface OmnigentLivePage {
  artifact_id: string;
  name: string;
  title: string | null;
  parent_session_id: string;
  task_id: string;
  rrule: string | null;
  paused: boolean;
  on_change: boolean;
  sources: string[];
  checked_at: number | null;
  failures: number;
  last_failure: { at: number; reason: string } | null;
  open_issue: string | null;
  last_update: { version: number | null; at: number | null; changes: number } | null;
}

/** `GET /v1/me/live-pages` — every live page of the caller, in one call. */
export async function listOmnigentLivePages(
  config: OmnigentClientConfig,
  email: string,
): Promise<OmnigentLivePage[]> {
  const response = await fetch(new URL("/v1/me/live-pages", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "list live pages", config.secrets);
  const body = (await response.json()) as { pages?: OmnigentLivePage[] };
  return body.pages ?? [];
}
