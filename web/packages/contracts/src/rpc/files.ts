import { oc } from "@orpc/contract";
import * as z from "zod";
import { ARTIFACT_NAME_MAX_LENGTH, ATTACHMENT_MAX_BASE64_LENGTH } from "../attachments.js";
import { HostStatusSchema } from "../domain.js";
import { Id } from "../ids.js";

import { ArtifactSchema } from "./artifacts.js";
import { botId } from "./shared.js";

export const filesContract = {
  host: {
    status: oc.input(botId).output(HostStatusSchema),
  },
  files: {
    list: oc.input(z.object({ botId: Id, path: z.string().max(1024).default("") })).output(
      z.object({
        entries: z.array(
          z.object({
            name: z.string(),
            path: z.string(),
            type: z.enum(["file", "directory"]),
            size: z.number().int().nullable(),
            modifiedAt: z.number().int(),
          }),
        ),
      }),
    ),
    read: oc.input(z.object({ botId: Id, path: z.string().min(1).max(1024) })).output(
      z.object({
        path: z.string(),
        name: z.string(),
        mimeType: z.string(),
        size: z.number().int(),
        /** Over the preview cap, or not text-like and not previewable: no content sent. */
        tooLarge: z.boolean(),
        binary: z.boolean(),
        contentBase64: z.string().nullable(),
      }),
    ),
    /** Writes a composer attachment into the Muse's workspace (`your_files/uploads/<date>/`). */
    uploadAttachment: oc
      .input(
        z.object({
          botId: Id,
          name: z.string().min(1).max(ARTIFACT_NAME_MAX_LENGTH),
          mimeType: z.string().min(1).max(200),
          contentBase64: z.string().min(1).max(ATTACHMENT_MAX_BASE64_LENGTH),
        }),
      )
      .output(
        z.object({
          path: z.string(),
          name: z.string(),
          mimeType: z.string(),
          size: z.number().int(),
        }),
      ),
    /** Reads an uploaded attachment in the host (Markdown, passages, embeddings, index) and
     * answers when it is searchable. The composer awaits it before Send is enabled. */
    ingestAttachment: oc.input(z.object({ botId: Id, path: z.string().min(1).max(1024) })).output(
      z.object({
        fileId: z.string(),
        name: z.string(),
        pages: z.number().int(),
        chars: z.number().int(),
      }),
    ),
    saveToLibrary: oc
      .input(z.object({ botId: Id, path: z.string().min(1).max(1024) }))
      .output(ArtifactSchema),
  },
};
