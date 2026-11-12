import { ORPCError } from "@orpc/server";
import { confineWorkspacePath, WorkspacePathError } from "@superchat/adapters/omnigent/browser";
import { isAllowedAttachmentMimeType, type WorkspaceAttachment } from "@superchat/contracts";
import { WORKSPACE_UPLOADS_PREFIX, type WorkspaceAttachmentRef } from "@superchat/core";

// biome-ignore lint/suspicious/noControlCharactersInRegex: a reference line must stay on one line
const CONTROL = /[\u0000-\u001f\u007f]/;

function confined(path: string): string {
  try {
    return confineWorkspacePath(path);
  } catch (error) {
    if (error instanceof WorkspacePathError) {
      throw new ORPCError("BAD_REQUEST", { message: error.message });
    }
    throw error;
  }
}

/** Checks the files a send refers to: each must be a confined path under `your_files/uploads/`
 * of an allowed type, so a malformed reference cannot point the Muse at another part of the
 * workspace or smuggle extra lines into the turn text. */
export function validateWorkspaceAttachments(
  attachments: readonly WorkspaceAttachment[],
): WorkspaceAttachmentRef[] {
  return attachments.map((attachment) => {
    const path = confined(attachment.path);
    if (
      path !== attachment.path ||
      !path.startsWith(WORKSPACE_UPLOADS_PREFIX) ||
      path.length === WORKSPACE_UPLOADS_PREFIX.length ||
      CONTROL.test(path)
    ) {
      throw new ORPCError("BAD_REQUEST", { message: "Attachment is not an uploaded file" });
    }
    if (!isAllowedAttachmentMimeType(attachment.mimeType)) {
      throw new ORPCError("BAD_REQUEST", {
        message: `Unsupported attachment type: ${attachment.mimeType}`,
      });
    }
    return { path, mimeType: attachment.mimeType, size: attachment.size };
  });
}

/** Folders a message points at: workspace-relative, confined, no control characters. */
export function validateWorkspaceFolders(folders: readonly string[]): string[] {
  return folders.map((folder) => {
    const path = confined(folder);
    if (CONTROL.test(path)) throw new ORPCError("BAD_REQUEST", { message: "Bad folder path" });
    return path;
  });
}
