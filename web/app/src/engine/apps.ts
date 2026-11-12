// `apps.*`: publish a saved HTML file at its own address. The viewer at `/apps/<slug>` is served
// by the engine itself; the page only manages the publication.
import { ORPCError } from "@orpc/server";
import {
  getOmnigentPublication,
  publishOmnigentArtifact,
  unpublishOmnigentArtifact,
} from "@superchat/adapters/omnigent/browser";
import { notFound, toPublish } from "./artifacts";
import { engineClient, NO_EMAIL } from "./client";
import { os } from "./context";

function publishError(error: unknown): never {
  if (error instanceof Error && /\(400\)/.test(error.message)) {
    throw new ORPCError("BAD_REQUEST", { message: "This file cannot be published" });
  }
  return notFound(error);
}

export const appsRouter = {
  apps: {
    // Only the owner's own file is ever found: the engine 404s another owner's artifact.
    publish: os.apps.publish.handler(async ({ input }) => {
      const published = await publishOmnigentArtifact(engineClient(), NO_EMAIL, input.artifactId, {
        audience: input.audience,
        version: input.version,
      }).catch(publishError);
      return toPublish(published);
    }),
    unpublish: os.apps.unpublish.handler(async ({ input }) => {
      await unpublishOmnigentArtifact(engineClient(), NO_EMAIL, input.artifactId).catch(notFound);
      return { ok: true as const };
    }),
    publishState: os.apps.publishState.handler(async ({ input }) => {
      const publication = await getOmnigentPublication(
        engineClient(),
        NO_EMAIL,
        input.artifactId,
      ).catch(notFound);
      return publication ? toPublish(publication) : null;
    }),
  },
};
