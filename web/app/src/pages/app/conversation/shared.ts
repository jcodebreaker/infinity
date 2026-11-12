import { ATTACHMENT_ALLOWED_MIME_TYPES } from "@superchat/contracts";

/** Where a Muse attachment is before its message can go: written to the host, waiting for a
 * sleeping host to wake, read there (converted, indexed), ready, or failed. Absent for chats
 * that do not read files first. */
export type AttachmentStatus = "uploading" | "starting" | "reading" | "ready" | "error";

export type PendingAttachment = {
  id: string;
  threadKey: string;
  file: File;
  previewUrl?: string;
  status?: AttachmentStatus;
  /** Calm copy for a failed attachment; the person can retry or remove it. */
  error?: string;
};

/** Send waits until every attachment that is being read is ready (a failed one is removed or
 * retried first). */
export function attachmentsBlockSend(attachments: readonly PendingAttachment[]): boolean {
  return attachments.some((attachment) => attachment.status && attachment.status !== "ready");
}

export const NO_ATTACHMENTS: PendingAttachment[] = [];
export const ATTACHMENT_ACCEPT = ATTACHMENT_ALLOWED_MIME_TYPES.join(",");

export function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      const base64 = result.includes(",") ? (result.split(",")[1] ?? "") : result;
      resolve(base64);
    };
    reader.onerror = () => reject(reader.error ?? new Error("Failed to read file"));
    reader.readAsDataURL(file);
  });
}
