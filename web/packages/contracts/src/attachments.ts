import * as z from "zod";

export const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;
export const ATTACHMENT_MAX_COUNT = 4;
export const ARTIFACT_NAME_MAX_LENGTH = 255;
export const ARTIFACT_DESCRIPTION_MAX_LENGTH = 280;
/** Base64 expands payload by 4/3; cap before decode to reject oversize uploads cheaply. */
export const ATTACHMENT_MAX_BASE64_LENGTH = Math.ceil(ATTACHMENT_MAX_BYTES / 3) * 4;

export const ATTACHMENT_IMAGE_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
] as const;

export const XLSX_MIME_TYPE =
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" as const;

export const ATTACHMENT_FILE_MIME_TYPES = [
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/csv",
  XLSX_MIME_TYPE,
  "text/html",
  "application/json",
] as const;

export const ATTACHMENT_ALLOWED_MIME_TYPES = [
  ...ATTACHMENT_IMAGE_MIME_TYPES,
  ...ATTACHMENT_FILE_MIME_TYPES,
] as const;

export type AttachmentMimeType = (typeof ATTACHMENT_ALLOWED_MIME_TYPES)[number];

export function isAttachmentImageMimeType(mimeType: string): boolean {
  return (ATTACHMENT_IMAGE_MIME_TYPES as readonly string[]).includes(mimeType);
}

export function isAllowedAttachmentMimeType(mimeType: string): mimeType is AttachmentMimeType {
  return (ATTACHMENT_ALLOWED_MIME_TYPES as readonly string[]).includes(mimeType);
}

/** A file in the Muse's workspace that a message refers to (uploaded by `files.uploadAttachment`). */
export const WorkspaceAttachmentSchema = z.object({
  path: z.string().min(1).max(1024),
  name: z.string().min(1).max(ARTIFACT_NAME_MAX_LENGTH),
  mimeType: z.string().min(1).max(200),
  size: z.number().int().nonnegative(),
});
export type WorkspaceAttachment = z.infer<typeof WorkspaceAttachmentSchema>;

export function validateThreadsSendInput(input: {
  text?: string;
  artifactIds?: string[];
  attachments?: unknown[];
}): boolean {
  const text = input.text?.trim() ?? "";
  const artifactIds = input.artifactIds ?? [];
  const attachments = input.attachments ?? [];
  return Boolean(text || artifactIds.length || attachments.length);
}
