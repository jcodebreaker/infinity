import {
  ATTACHMENT_MAX_BASE64_LENGTH,
  ATTACHMENT_MAX_BYTES,
  type AttachmentMimeType,
  isAllowedAttachmentMimeType,
  isAttachmentImageMimeType,
  type MessageBlock,
  XLSX_MIME_TYPE,
} from "@superchat/contracts";

export class AttachmentValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AttachmentValidationError";
  }
}

/**
 * Validates base64 without a grouped-repetition regex over the whole string.
 * `/^(?:[A-Za-z0-9+/]{4})*(...)?$/` stack-overflows V8's regex engine on
 * inputs in the megabytes-of-characters range — reproduced directly: a 5 MiB
 * string throws `RangeError: Maximum call stack size exceeded` on `.test()`.
 * At the current 10 MiB attachment limit, base64 expansion (4/3) puts any
 * attachment over ~3.75 MiB squarely in that range, so this crashes on a
 * routine, non-malicious upload. A flat, ungrouped `[chars]*` quantifier does
 * not have this problem; padding is checked separately since it can only
 * ever be the trailing 0-2 characters.
 */
function isWellFormedBase64(value: string): boolean {
  const paddingMatch = /={1,2}$/.exec(value);
  const paddingLength = paddingMatch ? paddingMatch[0].length : 0;
  const body = paddingLength > 0 ? value.slice(0, value.length - paddingLength) : value;
  if (body.includes("=")) return false;
  return /^[A-Za-z0-9+/]*$/.test(body);
}

export function decodeAttachmentBase64(contentBase64: string): Uint8Array {
  const normalized = contentBase64.trim();
  if (!normalized) throw new AttachmentValidationError("Attachment content is empty");
  if (normalized.length > ATTACHMENT_MAX_BASE64_LENGTH) {
    throw new AttachmentValidationError("Attachment exceeds the 10 MiB limit");
  }
  if (normalized.length % 4 !== 0 || !isWellFormedBase64(normalized)) {
    throw new AttachmentValidationError("Attachment content is not valid base64");
  }
  let bytes: Uint8Array;
  try {
    bytes = Uint8Array.from(atob(normalized), (char) => char.charCodeAt(0));
  } catch {
    throw new AttachmentValidationError("Attachment content is not valid base64");
  }
  if (bytes.byteLength === 0) throw new AttachmentValidationError("Attachment content is empty");
  if (bytes.byteLength > ATTACHMENT_MAX_BYTES) {
    throw new AttachmentValidationError("Attachment exceeds the 10 MiB limit");
  }
  return bytes;
}

export function validateAttachmentMimeType(mimeType: string): void {
  if (!isAllowedAttachmentMimeType(mimeType)) {
    throw new AttachmentValidationError(`Unsupported attachment type: ${mimeType}`);
  }
}

export function attachmentKindForMimeType(mimeType: string): "image" | "file" {
  return isAttachmentImageMimeType(mimeType) ? "image" : "file";
}

export function messageBlockForArtifact(artifact: {
  id: string;
  name: string;
  mimeType: string;
  size: number;
}): Extract<MessageBlock, { kind: "image" | "file" }> {
  if (attachmentKindForMimeType(artifact.mimeType) === "image") {
    return {
      kind: "image",
      artifactId: artifact.id,
      mimeType: artifact.mimeType,
      name: artifact.name,
    };
  }
  return {
    kind: "file",
    artifactId: artifact.id,
    mimeType: artifact.mimeType,
    name: artifact.name,
    size: artifact.size,
  };
}

export function promptTextForAttachments(
  text: string | undefined,
  artifacts: Array<{ name: string; mimeType: string; size: number }>,
): string {
  const caption = text?.trim() ?? "";
  const notes = artifacts
    .filter((artifact) => !isAttachmentImageMimeType(artifact.mimeType))
    .map(
      (artifact) =>
        `User attached file ${JSON.stringify(artifact.name)} (${artifact.mimeType}, ${artifact.size} bytes).`,
    );
  return [caption, ...notes].filter(Boolean).join("\n\n") || "See attached files.";
}

export function blocksToAgentHistoryText(blocks: MessageBlock[]): string {
  return blocks
    .map((block) => {
      if (block.kind === "text") return block.text;
      if (block.kind === "image") return `[image: ${block.name}]`;
      if (block.kind === "file") {
        return `[file: ${block.name} (${block.mimeType}, ${block.size} bytes)]`;
      }
      if ("text" in block && typeof block.text === "string") return block.text;
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

const EXTENSION_MIME_TYPES: Record<string, AttachmentMimeType> = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".pdf": "application/pdf",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".csv": "text/csv",
  ".xlsx": XLSX_MIME_TYPE,
  ".html": "text/html",
  ".htm": "text/html",
  ".json": "application/json",
};

const MIME_TYPE_EXTENSIONS: Record<AttachmentMimeType, string> = {
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/webp": ".webp",
  "image/gif": ".gif",
  "application/pdf": ".pdf",
  "text/plain": ".txt",
  "text/markdown": ".md",
  "text/csv": ".csv",
  [XLSX_MIME_TYPE]: ".xlsx",
  "text/html": ".html",
  "application/json": ".json",
};

export function inferAttachmentMimeType(
  name: string,
  reportedType?: string,
): AttachmentMimeType | null {
  const dot = name.lastIndexOf(".");
  const extensionType = dot < 0 ? undefined : EXTENSION_MIME_TYPES[name.slice(dot).toLowerCase()];
  // Some browsers and native document pickers report Markdown as text/plain.
  if (extensionType === "text/markdown" && (!reportedType || reportedType === "text/plain")) {
    return extensionType;
  }
  if (reportedType && isAllowedAttachmentMimeType(reportedType)) return reportedType;
  return extensionType ?? null;
}

export function attachmentExtensionForMimeType(mimeType: string): string {
  return isAllowedAttachmentMimeType(mimeType) ? MIME_TYPE_EXTENSIONS[mimeType] : "";
}

export function attachmentsForThread<T extends { threadKey: string }>(
  attachments: readonly T[],
  threadKey: string | undefined,
): T[] {
  if (!threadKey) return [];
  return attachments.filter((attachment) => attachment.threadKey === threadKey);
}

export function userTurnMessageForRun<
  T extends { id?: string; role: string; runId?: string | null; blocks: MessageBlock[] },
>(trigger: string, runId: string, messages: T[], sourceMessageId?: string | null): T | undefined {
  if (trigger !== "user") return undefined;
  return messages.find(
    (message) =>
      message.role === "user" &&
      (sourceMessageId ? message.id === sourceMessageId : message.runId === runId),
  );
}

// Muse chats keep a person's files in their host workspace, not in Superchat's database. A message
// refers to such a file with one reference line; this is the only definition of that format (the
// api writes it, the web and the worker parse it).

/** Where composer uploads land in the workspace. */
export const WORKSPACE_UPLOADS_PREFIX = "your_files/uploads/";

export interface WorkspaceAttachmentRef {
  path: string;
  mimeType: string;
  size: number;
}

const REFERENCE_PREFIX = "Attached file in your workspace: ";
const REFERENCE_LINE = /^Attached file in your workspace: (.+) \(([^(),\s]+), (\d+) bytes\)$/;

export function attachmentReferenceLine(ref: WorkspaceAttachmentRef): string {
  return `${REFERENCE_PREFIX}${ref.path} (${ref.mimeType}, ${ref.size} bytes)`;
}

// A folder attached to one message: a pointer only (its contents are never copied or inlined).
// The Muse hands the reading to a Helper so the folder's files never fill the Conversation.
const FOLDER_PREFIX = "Attached folder in your workspace: ";
const FOLDER_LINE = /^Attached folder in your workspace: (.*)\/$/;

/** The reference line for a workspace folder (workspace-relative, `""` is the whole workspace). */
export function folderReferenceLine(path: string): string {
  return `${FOLDER_PREFIX}${path}/`;
}

/** The attachment types the host reads (`files_ingest`): documents into Markdown and an
 * index, spreadsheets into a schema manifest (never row text). Images go to the model as images;
 * the other allowed types stay a path line. */
const INGESTABLE_MIME_TYPES: readonly string[] = [
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/csv",
  XLSX_MIME_TYPE,
];

export function isIngestableAttachmentMimeType(mimeType: string): boolean {
  return INGESTABLE_MIME_TYPES.includes(mimeType);
}

const CONTEXT_OPEN = "<attachment_context>";
const CONTEXT_CLOSE = "</attachment_context>";

/** The leading reference lines of a message and the caption after them. Lines that do not match
 * the format end the scan, so ordinary text is never mistaken for an attachment. A message stored
 * by an older gateway has an `<attachment_context>` block right after the lines; it is skipped
 * (it is document text for the model, not the person's words). Today the host builds that
 * context at turn start and it is never stored, so a message is only the lines and the caption. */
export function parseAttachmentReferences(text: string): {
  attachments: WorkspaceAttachmentRef[];
  folders: string[];
  caption: string;
} {
  const lines = text.split("\n");
  const attachments: WorkspaceAttachmentRef[] = [];
  const folders: string[] = [];
  let index = 0;
  for (; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    const folder = FOLDER_LINE.exec(line);
    if (folder) {
      folders.push(folder[1] ?? "");
      continue;
    }
    const match = REFERENCE_LINE.exec(line);
    if (!match) break;
    attachments.push({ path: match[1] ?? "", mimeType: match[2] ?? "", size: Number(match[3]) });
  }
  if (attachments.length === 0 && folders.length === 0)
    return { attachments, folders, caption: text };
  if (lines[index] === CONTEXT_OPEN) {
    const close = lines.indexOf(CONTEXT_CLOSE, index);
    if (close !== -1) index = close + 1;
  }
  return { attachments, folders, caption: lines.slice(index).join("\n").trim() };
}

/** The text a Muse turn carries for workspace attachments: one reference line each, a blank line,
 * then the caption. */
export function promptTextForWorkspaceAttachments(
  text: string | undefined,
  attachments: readonly WorkspaceAttachmentRef[],
  folders: readonly string[] = [],
): string {
  const caption = text?.trim() ?? "";
  const lines = [
    ...folders.map(folderReferenceLine),
    ...attachments.map(attachmentReferenceLine),
  ].join("\n");
  return [lines, caption].filter(Boolean).join("\n\n");
}
