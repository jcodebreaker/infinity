import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";

/** A daily note as the engine serves it (`routes/daily_notes.py` `note_to_response`). */
export interface OmnigentDailyNote {
  date: string;
  sections: Record<string, string>;
  edited_by_person: boolean;
  finalized: boolean;
  updated_at: number;
}

/** `GET /v1/me/daily-notes` — the person's notes, newest first. */
export async function listOmnigentDailyNotes(
  config: OmnigentClientConfig,
  email: string,
  limit = 14,
): Promise<OmnigentDailyNote[]> {
  const url = new URL("/v1/me/daily-notes", config.baseUrl);
  url.searchParams.set("limit", String(limit));
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "list daily notes", config.secrets);
  const body = (await response.json()) as { daily_notes?: OmnigentDailyNote[] };
  return body.daily_notes ?? [];
}

/** `PUT /v1/me/daily-notes/{date}` — the person's edit; the engine then never rewrites it. */
export async function putOmnigentDailyNote(
  config: OmnigentClientConfig,
  email: string,
  date: string,
  sections: Record<string, string>,
): Promise<OmnigentDailyNote> {
  const response = await fetch(
    new URL(`/v1/me/daily-notes/${encodeURIComponent(date)}`, config.baseUrl),
    {
      method: "PUT",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({ sections }),
    },
  );
  await throwOnError(response, "save daily note", config.secrets);
  return (await response.json()) as OmnigentDailyNote;
}
