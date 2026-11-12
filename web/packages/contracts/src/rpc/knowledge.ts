import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

/** Where a file is in the index: still being read, searchable, or couldn't be read. */
export const KNOWLEDGE_FILE_STATES = ["indexing", "searchable", "failed"] as const;
export const KnowledgeFileStateSchema = z.enum(KNOWLEDGE_FILE_STATES);
export type KnowledgeFileState = z.infer<typeof KnowledgeFileStateSchema>;

/** One indexed file's state (engine `GET /v1/sessions/{id}/knowledge/status`). */
export const KnowledgeFileSchema = z.object({
  /** The host index's id for the file (a hex digest of its workspace path). */
  fileId: z.string(),
  /** Workspace-relative path, e.g. `your_files/uploads/2026-10-09/report.pdf`. */
  path: z.string(),
  name: z.string(),
  kind: z.string(),
  state: KnowledgeFileStateSchema,
  /** `keyword` until the person has a connection that can embed (see `keywordOnlyReason`). */
  search: z.enum(["hybrid", "keyword"]),
  keywordOnlyReason: z.string().nullable(),
  pages: z.number().int(),
  /** PDF pages with no text layer (scans): not searchable until the deep parse reads them. */
  pagesWithoutText: z.number().int(),
  error: z.string().nullable(),
  /** Set when the file is also a Library artifact. */
  artifactId: Id.nullable(),
});
export type KnowledgeFile = z.infer<typeof KnowledgeFileSchema>;

export const KnowledgeStatusSchema = z.object({
  files: z.array(KnowledgeFileSchema),
  embeddings: z.object({ available: z.boolean(), reason: z.string().nullable() }),
  /** `asleep`: the files are the last-known snapshot (possibly empty); status never wakes it. */
  host: z.enum(["awake", "asleep"]),
  updatedAt: z.number().nullable(),
});
export type KnowledgeStatus = z.infer<typeof KnowledgeStatusSchema>;

export const KnowledgePassageSchema = z.object({
  fileName: z.string(),
  fileId: z.string(),
  path: z.string(),
  artifactId: Id.nullable(),
  page: z.number().int(),
  pageEnd: z.number().int(),
  score: z.number(),
  heading: z.string(),
  passage: z.string(),
  hasThumbnail: z.boolean(),
});
export type KnowledgePassage = z.infer<typeof KnowledgePassageSchema>;

export const knowledgeContract = {
  knowledge: {
    /** Per-file index state, and whether searches can use embeddings. */
    status: oc.input(z.object({})).output(KnowledgeStatusSchema),
    /** Passages from the person's files, best first. */
    search: oc
      .input(
        z.object({
          query: z.string().min(1).max(2000),
          fileIds: z
            .array(z.string().regex(/^[0-9a-f]{8,128}$/))
            .max(200)
            .optional(),
          k: z.number().int().min(1).max(25).optional(),
        }),
      )
      .output(
        z.object({
          mode: z.enum(["hybrid", "keyword"]),
          keywordOnlyReason: z.string().nullable(),
          filesIndexing: z.number().int(),
          /** Which files are still being indexed, so the answer can say what it may be missing. */
          filesIndexingNames: z.array(z.string()),
          results: z.array(KnowledgePassageSchema),
        }),
      ),
    /** A page's 256 px WebP thumbnail (a PDF's citation chip). */
    pageThumbnail: oc
      .input(
        z.object({ fileId: z.string().regex(/^[0-9a-f]{8,128}$/), page: z.number().int().min(1) }),
      )
      .output(z.object({ contentBase64: z.string(), mimeType: z.literal("image/webp") })),
    /** Re-embed files that were indexed without embeddings (the person has since added a key). */
    reindex: oc.input(z.object({})).output(z.object({ queued: z.number().int() })),
  },
};
