import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";
import { ArtifactPublishAudienceSchema, ArtifactPublishSchema } from "./artifacts.js";

export const appsContract = {
  apps: {
    publish: oc
      .input(
        z.object({
          artifactId: Id,
          audience: ArtifactPublishAudienceSchema,
          version: z.number().int().min(1).optional(),
        }),
      )
      .output(ArtifactPublishSchema),
    unpublish: oc.input(z.object({ artifactId: Id })).output(z.object({ ok: z.literal(true) })),
    publishState: oc.input(z.object({ artifactId: Id })).output(ArtifactPublishSchema.nullable()),
  },
};
