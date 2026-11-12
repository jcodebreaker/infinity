// Decks on the Omnigent side: a saved `.deck.html` exported to editable PowerPoint or vector PDF
// in the person's host (engine/omnigent/superchat/decks/routes.py). The result is a
// new artifact row in the deck's chat.
import type { OmnigentArtifact } from "./artifacts.js";
import { type OmnigentClientConfig, omnigentHeaders, throwOnError } from "./client.js";

/** The engine's wake plus the export itself can take a couple of minutes. */
const EXPORT_TIMEOUT_MS = 240_000;

/** `POST /decks/{id}/export` — exports the deck and returns the new artifact. */
export async function exportOmnigentDeck(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  format: "pptx" | "pdf",
): Promise<OmnigentArtifact> {
  const response = await fetch(
    new URL(`/v1/decks/${encodeURIComponent(id)}/export`, config.baseUrl),
    {
      method: "POST",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({ format }),
      signal: AbortSignal.timeout(EXPORT_TIMEOUT_MS),
    },
  );
  await throwOnError(response, "export deck", config.secrets);
  return (await response.json()) as OmnigentArtifact;
}

/** One theme of the dictionary (`GET /decks/themes`). */
export interface OmnigentDeckTheme {
  id: string;
  name: string;
  /** Two to four words: what a picker tile says under the thumbnail. */
  tagline: string;
  mood: string;
  category: "professional" | "editorial" | "bold" | "dark";
  mode: "light" | "dark";
  best_for: string;
  preview: string;
}

/** `GET /decks/themes` — the deck theme dictionary with thumbnails. */
export async function listOmnigentDeckThemes(
  config: OmnigentClientConfig,
  email: string,
): Promise<{ themes: OmnigentDeckTheme[]; default: string }> {
  const response = await fetch(new URL("/v1/decks/themes", config.baseUrl), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "list deck themes", config.secrets);
  return (await response.json()) as { themes: OmnigentDeckTheme[]; default: string };
}

/** `GET /decks/themes/{id}/sample` — the kit's sample deck built in one theme (a `.deck.html`),
 * for a theme preview. */
export async function getOmnigentDeckThemeSample(
  config: OmnigentClientConfig,
  email: string,
  themeId: string,
): Promise<string> {
  const response = await fetch(
    new URL(`/v1/decks/themes/${encodeURIComponent(themeId)}/sample`, config.baseUrl),
    { headers: omnigentHeaders(config, email) },
  );
  await throwOnError(response, "read deck theme sample", config.secrets);
  return ((await response.json()) as { html: string }).html;
}

/** `GET /decks/{id}/theme` — the theme id a deck is on, `null` when hand-made. */
export async function getOmnigentDeckTheme(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<string | null> {
  const response = await fetch(
    new URL(`/v1/decks/${encodeURIComponent(id)}/theme`, config.baseUrl),
    {
      headers: omnigentHeaders(config, email),
    },
  );
  await throwOnError(response, "read deck theme", config.secrets);
  return ((await response.json()) as { theme: string | null }).theme;
}
