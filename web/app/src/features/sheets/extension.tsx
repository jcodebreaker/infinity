import type { ArtifactExtension } from "../artifacts";
import { sheetEditorKind } from "./SheetView";
import { SHEET_MIME_TYPES } from "./sheet-model";
// Reads a sent selection back into its chip in the transcript.
import "./sheet-ask";

/** Opens CSV / XLSX files in the sheet editor and gives its panel the wider width. */
export const sheetsExtension: ArtifactExtension = {
  usePanel: ({ artifact }) => ({ wide: !!artifact && SHEET_MIME_TYPES.has(artifact.mimeType) }),
  card: () => null,
  editor: (artifact) => (SHEET_MIME_TYPES.has(artifact.mimeType) ? sheetEditorKind : null),
};
