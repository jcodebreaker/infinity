// `artifacts.*`: the deliverables the Muse saved with `artifact_save` (engine/omnigent/
// superchat/artifacts). The engine rows are mapped onto the shapes the Library and preview speak,
// with the engine's artifact id standing in for both the family id and the version id.
import { ORPCError } from "@orpc/server";
import {
  bytesToBase64,
  createChatOwnershipResolver,
  deleteOmnigentArtifact,
  editOmnigentArtifact,
  fetchOmnigentArtifactContent,
  getOmnigentArtifact,
  listOmnigentArtifacts,
  OmnigentApiError,
  openOmnigentArtifactChat,
  restoreOmnigentArtifact,
  signalOmnigentArtifactChatOpen,
  versionMessageId,
} from "@superchat/adapters/omnigent/browser";
import type { Artifact } from "@superchat/contracts";
import { toArtifact } from "./artifact-shape";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { os } from "./context";

export { toPublish } from "./artifact-shape";

const iso = (epochSeconds: number): string => new Date(epochSeconds * 1000).toISOString();

/** The engine's page cap for `GET /v1/artifacts` (it has no offset). */
const ENGINE_LIST_MAX = 500;
const DEFAULT_LIST_LIMIT = 200;

/**
 * Maps an engine session to the Muse it belongs to. A deliverable is keyed by the chat that saved
 * it (`artifact_save` by a Helper lands on the chat that launched it), so besides the Muse's own
 * Conversation its Helpers (any depth) count: the adapter's one ownership rule, built once per
 * request. Returns the Muse's id (its session id) or undefined.
 */
async function ownerResolver(): Promise<(sessionId: string) => Promise<string | undefined>> {
  const muse = await museSessionId();
  const resolve = createChatOwnershipResolver(engineClient(), NO_EMAIL, [
    { botId: muse, omnigentSessionId: muse },
  ]);
  return async (sessionId) => (sessionId === muse ? muse : (await resolve(sessionId))?.botId);
}

async function listWithCounts(limit: number): Promise<Array<Artifact & { versionCount: number }>> {
  // The engine can only scope by one chat, and the Muse's deliverables span its Helpers, so take
  // its newest page and attribute each row.
  const [items, ownerOf] = await Promise.all([
    listOmnigentArtifacts(engineClient(), NO_EMAIL, undefined, ENGINE_LIST_MAX),
    ownerResolver(),
  ]);
  const owned: Array<Artifact & { versionCount: number }> = [];
  for (const item of items) {
    const owner = await ownerOf(item.parent_session_id);
    if (!owner) continue;
    owned.push({ ...toArtifact(item, owner), versionCount: item.versions });
    if (owned.length === limit) break;
  }
  return owned;
}

export function notFound(error: unknown): never {
  if (error instanceof Error && /\(404\)/.test(error.message)) {
    throw new ORPCError("NOT_FOUND", { message: "Artifact not found" });
  }
  throw error;
}

export const artifactsRouter = {
  artifacts: {
    list: os.artifacts.list.handler(() => listWithCounts(DEFAULT_LIST_LIMIT)),
    // The engine pages by newest-first cap only, so there is never a next page.
    listSpace: os.artifacts.listSpace.handler(async ({ input }) => ({
      items: await listWithCounts(input.limit ?? DEFAULT_LIST_LIMIT),
      nextCursor: null,
    })),
    listVersions: os.artifacts.listVersions.handler(async ({ input }) => {
      const [detail, conversationId] = await Promise.all([
        getOmnigentArtifact(engineClient(), NO_EMAIL, input.familyId).catch(notFound),
        museSessionId(),
      ]);
      return detail.all_versions.map((version) => ({
        id: version.id,
        version: version.version,
        name: detail.name,
        createdAt: iso(version.created_at),
        origin: version.origin,
        parentVersionId: version.parent_version_id ?? null,
        editSummary: version.edit_summary ?? null,
        chatId: version.chat_id ?? null,
        itemId: versionMessageId(version, conversationId),
        fromStudio: version.from_studio ?? false,
      }));
    }),
    getById: os.artifacts.getById.handler(async ({ input }) => {
      const detail = await getOmnigentArtifact(engineClient(), NO_EMAIL, input.artifactId).catch(
        notFound,
      );
      const bytes = await fetchOmnigentArtifactContent(engineClient(), NO_EMAIL, input.artifactId);
      const ownerOf = await ownerResolver();
      return {
        ...toArtifact(detail, (await ownerOf(detail.parent_session_id)) ?? null),
        contentBase64: bytesToBase64(bytes),
      };
    }),
    // The file's studio chat (ADR 0024): one per file, made on first open.
    chat: os.artifacts.chat.handler(async ({ input }) => {
      const opened = await openOmnigentArtifactChat(
        engineClient(),
        NO_EMAIL,
        input.artifactId,
      ).catch(notFound);
      return {
        sessionId: opened.session_id,
        created: opened.created,
        prelude: (opened.prelude ?? []).map((row) => ({
          version: row.version,
          roundId: row.round_id ?? null,
          ask: row.ask ?? "",
          handback: row.handback ?? "",
          itemId: row.item_id ?? null,
          at: typeof row.at === "number" ? new Date(row.at * 1000).toISOString() : row.at,
        })),
      };
    }),
    chatOpen: os.artifacts.chatOpen.handler(async ({ input }) => {
      const opened = await signalOmnigentArtifactChatOpen(
        engineClient(),
        NO_EMAIL,
        input.artifactId,
      ).catch(notFound);
      return { sessionId: opened.session_id, warm: opened.warm };
    }),
    restore: os.artifacts.restore.handler(async ({ input }) => {
      const created = await restoreOmnigentArtifact(
        engineClient(),
        NO_EMAIL,
        input.artifactId,
      ).catch(notFound);
      const ownerOf = await ownerResolver();
      return toArtifact(created, (await ownerOf(created.parent_session_id)) ?? null);
    }),
    // A stale base (`artifact_stale`) and a patch the engine will not apply exactly (`conflict`)
    // are both CONFLICT; `data.code` tells the editor which (it reloads, or offers superchat).
    edit: os.artifacts.edit.handler(async ({ input }) => {
      try {
        const created = await editOmnigentArtifact(
          engineClient(),
          NO_EMAIL,
          input.artifactId,
          input.baseVersion,
          input.patches,
        );
        return toArtifact(created, null);
      } catch (error) {
        if (error instanceof OmnigentApiError) {
          if (error.code === "artifact_stale" || error.code === "conflict") {
            throw new ORPCError("CONFLICT", {
              message: error.detail ?? "That edit couldn't be saved.",
              data: { code: error.code },
            });
          }
          if (error.code === "invalid_input") {
            throw new ORPCError("BAD_REQUEST", {
              message: error.detail ?? "That edit isn't allowed.",
            });
          }
        }
        return notFound(error);
      }
    }),
    remove: os.artifacts.remove.handler(async ({ input }) => {
      await deleteOmnigentArtifact(engineClient(), NO_EMAIL, input.artifactId).catch(notFound);
      return { ok: true as const };
    }),
  },
};
