// The person's file search on the Omnigent side. The index lives in the person's host; the
// engine only relays it through the Muse session's routes (`/v1/sessions/{id}/knowledge/*`).
// Plain fetch client.
import { type OmnigentClientConfig, omnigentHeaders, throwOnError } from "./client/core.js";

/** One file's index state (`GET /v1/sessions/{id}/knowledge/status`). */
export interface OmnigentKnowledgeFile {
  file_id: string;
  /** Workspace-relative, e.g. `your_files/uploads/2026-10-09/report.pdf`. */
  path: string;
  name: string;
  kind: string;
  state: "indexing" | "searchable" | "failed";
  /** `"keyword"` when the person has no connection that can embed. */
  search: "hybrid" | "keyword";
  keyword_only_reason: string | null;
  pages: number;
  pages_without_text: number;
  error: string | null;
  /** Set when the file is also a Library artifact. */
  artifact_id: string | null;
}

export interface OmnigentKnowledgeStatus {
  files: OmnigentKnowledgeFile[];
  embeddings: { available: boolean; reason: string | null };
  /** `asleep`: `files` is the last-known snapshot; the call never wakes the host. */
  host: "awake" | "asleep";
  updated_at: number | null;
}

/** One passage a search found (`POST /v1/sessions/{id}/knowledge/search`). */
export interface OmnigentKnowledgePassage {
  file_id: string;
  path: string;
  file_name: string;
  artifact_id: string | null;
  page: number;
  page_end: number;
  score: number;
  heading: string;
  passage: string;
  thumbnail_url: string | null;
}

export interface OmnigentKnowledgeSearch {
  mode: "hybrid" | "keyword";
  keyword_only_reason: string | null;
  files_indexing: number;
  /** Names of the files still being indexed (the answer may be missing them). */
  files_indexing_names?: string[];
  results: OmnigentKnowledgePassage[];
}

function knowledgeUrl(config: OmnigentClientConfig, sessionId: string, tail: string): URL {
  return new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/knowledge${tail}`, config.baseUrl);
}

/** `GET .../knowledge/status` — the Muse's files and whether searches can embed. */
export async function getOmnigentKnowledgeStatus(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<OmnigentKnowledgeStatus> {
  const response = await fetch(knowledgeUrl(config, sessionId, "/status"), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "get knowledge status", config.secrets);
  return (await response.json()) as OmnigentKnowledgeStatus;
}

/** `POST .../knowledge/search` — passages from the Muse's files, best first (wakes the host). */
export async function searchOmnigentKnowledge(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  input: { query: string; fileIds?: string[]; k?: number },
): Promise<OmnigentKnowledgeSearch> {
  const response = await fetch(knowledgeUrl(config, sessionId, "/search"), {
    method: "POST",
    headers: { ...omnigentHeaders(config, email), "content-type": "application/json" },
    body: JSON.stringify({ query: input.query, file_ids: input.fileIds, k: input.k }),
  });
  await throwOnError(response, "search files", config.secrets);
  return (await response.json()) as OmnigentKnowledgeSearch;
}

/** `GET .../knowledge/files/{file_id}/pages/{n}/thumbnail` — the page's WebP bytes. */
export async function fetchOmnigentPageThumbnail(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  fileId: string,
  page: number,
): Promise<Uint8Array> {
  const url = knowledgeUrl(
    config,
    sessionId,
    `/files/${encodeURIComponent(fileId)}/pages/${page}/thumbnail`,
  );
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "read page thumbnail", config.secrets);
  return new Uint8Array(await response.arrayBuffer());
}

/** `POST .../knowledge/reindex` — re-embed files indexed without embeddings (0 when asleep). */
export async function reindexOmnigentKnowledge(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<{ queued: number }> {
  const response = await fetch(knowledgeUrl(config, sessionId, "/reindex"), {
    method: "POST",
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "reindex files", config.secrets);
  return (await response.json()) as { queued: number };
}

/** What reading one file came to (`POST .../knowledge/ingest`). */
export interface OmnigentKnowledgeIngest {
  file_id: string;
  name: string;
  path: string;
  pages: number;
  pages_without_text: number;
  chars: number;
  /** The file's kind (`pdf`, `txt`, `md`, `csv`, `xlsx`); a table's `markdown` is its manifest. */
  kind?: string;
  /** Where the Markdown version lives in the host. */
  markdown_path: string;
  search: "hybrid" | "keyword";
}

/**
 * `POST .../knowledge/ingest` — reads one workspace file through every pass (Markdown, passages,
 * embeddings, index) and answers when it is searchable. Wakes the host. Idempotent.
 */
export async function ingestOmnigentKnowledge(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  input: { path: string },
): Promise<OmnigentKnowledgeIngest> {
  const response = await fetch(knowledgeUrl(config, sessionId, "/ingest"), {
    method: "POST",
    headers: { ...omnigentHeaders(config, email), "content-type": "application/json" },
    body: JSON.stringify({ path: input.path }),
  });
  await throwOnError(response, "read file", config.secrets);
  return (await response.json()) as OmnigentKnowledgeIngest;
}

export interface OmnigentKnowledgeFound {
  found: boolean;
  file_id?: string;
  name?: string;
  path?: string;
  pages?: number;
}

/**
 * `POST .../knowledge/find` — the searchable upload that already holds exactly these bytes
 * (SHA-256, hex), if the host has one. Wakes the host. A new attachment of a file that is
 * already stored reuses it and its index entry.
 */
export async function findOmnigentKnowledge(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  input: { sha256: string },
): Promise<OmnigentKnowledgeFound> {
  const response = await fetch(knowledgeUrl(config, sessionId, "/find"), {
    method: "POST",
    headers: { ...omnigentHeaders(config, email), "content-type": "application/json" },
    body: JSON.stringify({ sha256: input.sha256 }),
  });
  await throwOnError(response, "find file", config.secrets);
  return (await response.json()) as OmnigentKnowledgeFound;
}
