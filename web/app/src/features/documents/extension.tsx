import type { ArtifactExtension } from "../artifacts";
import { DocReader, docEditorKind } from "./DocEditor";

const isMarkdown = (mimeType: string) => mimeType === "text/markdown";

/** Opens markdown files in the document editor, and shows them as a page wherever a file is
 * previewed. */
export const documentsExtension: ArtifactExtension = {
  usePanel: () => ({}),
  card: () => null,
  view: ({ artifact, bytes }) =>
    isMarkdown(artifact.mimeType) ? <DocReader bytes={bytes} /> : null,
  editor: (artifact) => (isMarkdown(artifact.mimeType) ? docEditorKind : null),
};
