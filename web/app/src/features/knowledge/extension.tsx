import type { Artifact } from "@superchat/contracts";
import type { ArtifactExtension } from "../artifacts";
import { IndexBadge } from "./IndexBadge";

/** What the engine indexes: PDF, text, Markdown, CSV and XLSX files. */
const INDEXED_MIME_TYPES = new Set([
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/csv",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]);

/** Library cards show whether a file is searchable. */
export const knowledgeExtension: ArtifactExtension = {
  usePanel: () => ({}),
  card: (artifact: Artifact) =>
    INDEXED_MIME_TYPES.has(artifact.mimeType)
      ? { badge: <IndexBadge artifactId={artifact.id} /> }
      : null,
};
