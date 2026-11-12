import { ORPCError } from "@orpc/server";
import {
  bytesToBase64,
  fetchOmnigentPageThumbnail,
  getOmnigentKnowledgeStatus,
  reindexOmnigentKnowledge,
  searchOmnigentKnowledge,
} from "@superchat/adapters/omnigent/browser";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { os } from "./context";

function pageNotFound(error: unknown): never {
  if (error instanceof Error && /\(404\)/.test(error.message)) {
    throw new ORPCError("NOT_FOUND", { message: "Page not found" });
  }
  throw error;
}

/** `knowledge.*`: the index state of the person's files, search and page thumbnails, relayed
 * from the Muse session's `/knowledge/*` routes. */
export const knowledgeRouter = {
  knowledge: {
    status: os.knowledge.status.handler(async () => {
      const status = await getOmnigentKnowledgeStatus(
        engineClient(),
        NO_EMAIL,
        await museSessionId(),
      );
      return {
        files: status.files.map((file) => ({
          fileId: file.file_id,
          path: file.path,
          name: file.name,
          kind: file.kind,
          state: file.state,
          search: file.search,
          keywordOnlyReason: file.keyword_only_reason,
          pages: file.pages,
          pagesWithoutText: file.pages_without_text,
          error: file.error,
          artifactId: file.artifact_id,
        })),
        embeddings: status.embeddings,
        host: status.host,
        updatedAt: status.updated_at,
      };
    }),
    search: os.knowledge.search.handler(async ({ input }) => {
      const found = await searchOmnigentKnowledge(
        engineClient(),
        NO_EMAIL,
        await museSessionId(),
        input,
      );
      return {
        mode: found.mode,
        keywordOnlyReason: found.keyword_only_reason,
        filesIndexing: found.files_indexing,
        filesIndexingNames: found.files_indexing_names ?? [],
        results: found.results.map((hit) => ({
          fileName: hit.file_name,
          fileId: hit.file_id,
          path: hit.path,
          artifactId: hit.artifact_id,
          page: hit.page,
          pageEnd: hit.page_end,
          score: hit.score,
          heading: hit.heading,
          passage: hit.passage,
          hasThumbnail: hit.thumbnail_url !== null,
        })),
      };
    }),
    pageThumbnail: os.knowledge.pageThumbnail.handler(async ({ input }) => {
      const bytes = await fetchOmnigentPageThumbnail(
        engineClient(),
        NO_EMAIL,
        await museSessionId(),
        input.fileId,
        input.page,
      ).catch(pageNotFound);
      return { contentBase64: bytesToBase64(bytes), mimeType: "image/webp" as const };
    }),
    reindex: os.knowledge.reindex.handler(async () =>
      reindexOmnigentKnowledge(engineClient(), NO_EMAIL, await museSessionId()),
    ),
  },
};
