import { ORPCError } from "@orpc/server";
import type { OmnigentArtifact, OmnigentPublication } from "@superchat/adapters/omnigent/browser";
import type { Artifact, ArtifactPublish } from "@superchat/contracts";

const iso = (epochSeconds: number): string => new Date(epochSeconds * 1000).toISOString();

/** The served content type without parameters (`text/html; charset=utf-8` -> `text/html`). */
const baseMime = (mime: string): string => mime.split(";")[0]?.trim() || mime;

export function toPublish(publication: OmnigentPublication): ArtifactPublish {
  return {
    slug: publication.slug,
    urlPath: publication.url_path,
    audience: publication.audience,
    version: publication.version,
    publishedAt: iso(publication.published_at),
    stats: {
      opensTotal: publication.stats.opens_total,
      uniqueViewers: publication.stats.unique_viewers,
      opens7d: publication.stats.opens_7d,
    },
  };
}

/** An engine artifact row as the Library and previews speak it; the engine's artifact id stands
 * in for both the family id and the version id. */
export function toArtifact(item: OmnigentArtifact, botId: string | null): Artifact {
  return {
    publish: item.publish ? toPublish(item.publish) : null,
    id: item.id,
    botId,
    runId: null,
    name: item.name,
    description: item.title,
    mimeType: baseMime(item.mime),
    size: item.size,
    version: item.version,
    createdAt: iso(item.created_at),
    origin: item.origin,
    chatId: item.chat_id ?? null,
    groupId: item.group_id ?? null,
  };
}

/** An engine 404 reads as a missing artifact; every other failure passes through. */
export function artifactNotFound(error: unknown): never {
  if (error instanceof Error && /\(404\)/.test(error.message)) {
    throw new ORPCError("NOT_FOUND", { message: "Artifact not found" });
  }
  throw error;
}
