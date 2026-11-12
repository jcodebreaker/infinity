// Sheets on the Omnigent side: a CSV / XLSX artifact read as grids and hand-edited
// (engine/omnigent/superchat/sheets/routes.py). An edit saves a new artifact version,
// so the result is an artifact row.
import type { OmnigentArtifact } from "./artifacts.js";
import { type OmnigentClientConfig, omnigentHeaders, throwOnError } from "./client.js";

function artifactUrl(config: OmnigentClientConfig, id: string, suffix = ""): URL {
  return new URL(`/v1/artifacts/${encodeURIComponent(id)}${suffix}`, config.baseUrl);
}

/** One cell of a table artifact; `v` is null for a formula with no cached value. */
export interface OmnigentTableCell {
  v: string | number | boolean | null;
  f: string | null;
  t: string | null;
  z?: string;
}

export interface OmnigentTableSheet {
  name: string;
  rows: OmnigentTableCell[][];
  frozen_rows: number;
  n_rows: number;
  n_cols: number;
  truncated: boolean;
  col_widths?: Array<number | null>;
}

export interface OmnigentTable {
  artifact_id: string;
  version: number;
  kind: string;
  sheets: OmnigentTableSheet[];
}

export interface OmnigentTableEdit {
  sheet: string;
  row: number;
  col: number;
  value: string | number | boolean | null;
}

/** `PATCH /artifacts/{id}/table` answered 409: the newest version moved on. */
export class OmnigentTableConflictError extends Error {
  constructor() {
    super("The sheet changed since it was opened.");
    this.name = "OmnigentTableConflictError";
  }
}

/** `GET /artifacts/{id}/table` — a CSV / XLSX version as grids. */
export async function getOmnigentTable(
  config: OmnigentClientConfig,
  email: string,
  id: string,
): Promise<OmnigentTable> {
  const response = await fetch(artifactUrl(config, id, "/table"), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "read table", config.secrets);
  return (await response.json()) as OmnigentTable;
}

/** `PATCH /artifacts/{id}/table` — cell edits as a new `manual` version. Throws
 * `OmnigentTableConflictError` on 409 (`baseVersion` is stale). */
export async function editOmnigentTable(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  baseVersion: number,
  edits: OmnigentTableEdit[],
): Promise<OmnigentArtifact> {
  const response = await fetch(artifactUrl(config, id, "/table"), {
    method: "PATCH",
    headers: omnigentHeaders(config, email),
    body: JSON.stringify({ base_version: baseVersion, edits }),
  });
  if (response.status === 409) throw new OmnigentTableConflictError();
  await throwOnError(response, "edit table", config.secrets);
  return (await response.json()) as OmnigentArtifact;
}

/** `GET /artifacts/{id}/table/range` — the untrusted-data excerpt of a selection. */
export async function getOmnigentTableRange(
  config: OmnigentClientConfig,
  email: string,
  id: string,
  sheet: string,
  range: string,
): Promise<{ version: number; block: string }> {
  const url = artifactUrl(config, id, "/table/range");
  url.searchParams.set("sheet", sheet);
  url.searchParams.set("range", range);
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "read table range", config.secrets);
  return (await response.json()) as { version: number; block: string };
}
