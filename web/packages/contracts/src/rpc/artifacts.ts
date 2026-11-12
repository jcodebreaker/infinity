import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

import { botId } from "./shared.js";

export const ARTIFACT_PUBLISH_AUDIENCES = ["owner", "org", "link"] as const;
export const ArtifactPublishAudienceSchema = z.enum(ARTIFACT_PUBLISH_AUDIENCES);
export type ArtifactPublishAudience = z.infer<typeof ArtifactPublishAudienceSchema>;

/** A published app (the apps capability builds on this shape; it rides on artifact rows): its address segment, who can open it, the pinned version and view counts. */
export const ArtifactPublishSchema = z.object({
  slug: z.string(),
  /** The path the app is served at, e.g. `/apps/budget-k3m9xq`. */
  urlPath: z.string(),
  audience: ArtifactPublishAudienceSchema,
  version: z.number().int(),
  publishedAt: z.string(),
  stats: z.object({
    opensTotal: z.number().int(),
    uniqueViewers: z.number().int(),
    opens7d: z.number().int(),
  }),
});
export type ArtifactPublish = z.infer<typeof ArtifactPublishSchema>;

export const ArtifactSchema = z.object({
  id: Id,
  botId: Id.nullable(),
  runId: Id.nullable(),
  name: z.string(),
  description: z.string().nullable(),
  mimeType: z.string(),
  size: z.number().int(),
  version: z.number().int(),
  createdAt: z.string(),
  /** Set when the file is published as an app. */
  publish: ArtifactPublishSchema.nullish(),
  /** How this version came to be: "ai", "manual", "restore" or "update" (a live update). */
  origin: z.string().optional(),
  /** The chat that saved this version (a Helper or update run for theirs), if known. */
  chatId: Id.nullable().optional(),
  /** The file's stable id, the same on every version (ADR 0024). */
  groupId: Id.nullish(),
});

export type Artifact = z.infer<typeof ArtifactSchema>;

export const ArtifactVersionSchema = z.object({
  id: Id,
  version: z.number().int(),
  name: z.string(),
  createdAt: z.string(),
  /** "ai" (the Muse saved it), "manual" (edited by hand) or "restore" (an earlier version made
   * newest again). */
  origin: z.string().optional(),
  parentVersionId: Id.nullable().optional(),
  editSummary: z.string().nullable().optional(),
  /** The chat that saved it (a Helper or update run for theirs); null before this was kept. */
  chatId: Id.nullable().optional(),
  /** The person's message whose turn saved it; null for a version made by hand. In the file's
   * studio chat when `fromStudio`, else in the Conversation. */
  itemId: Id.nullable().optional(),
  /** Saved from the file's own studio chat (ADR 0024). */
  fromStudio: z.boolean().optional(),
});

export type ArtifactVersion = z.infer<typeof ArtifactVersionSchema>;

/** One version of the file made in Super Chat, as its studio's opening rows (ADR 0030): the
 * person's ask and the Assistant's hand-back in the Round that saved it, read from the
 * Conversation, never copied. */
export const StudioPreludeSchema = z.object({
  version: z.number().int(),
  roundId: z.string().nullable(),
  /** The person's words that opened the Round. */
  ask: z.string(),
  /** The Assistant's last words in that Round. */
  handback: z.string(),
  /** The Conversation message to scroll to ("Show in Super Chat"). */
  itemId: z.string().nullable(),
  /** When the person asked (ISO). */
  at: z.string(),
});
export type StudioPrelude = z.infer<typeof StudioPreludeSchema>;

export const ArtifactWithContentSchema = ArtifactSchema.extend({
  contentBase64: z.string(),
});
export type ArtifactWithContent = z.infer<typeof ArtifactWithContentSchema>;

const ElementId = z.string().min(1).max(128);

/** One source patch of an HTML file (a deck, a page), applied by the engine on `data-superchat-id`
 * (same shape on the wire). `set-theme` is a deck's only. */
export const HtmlPatchSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("set-text"), id: ElementId, text: z.string().max(20_000) }),
  z.object({
    kind: z.literal("set-style"),
    id: ElementId,
    /** Kebab-case CSS property -> value; null removes the declaration. */
    style: z.record(z.string().regex(/^[a-z-]{1,64}$/), z.string().max(512).nullable()),
    /** What the editor showed before, only to word "you changed X" (never written to the file). */
    before: z.record(z.string().regex(/^[a-z-]{1,64}$/), z.string().max(200)).optional(),
  }),
  z.object({
    kind: z.literal("set-attributes"),
    id: ElementId,
    attributes: z.object({
      href: z.string().max(2048).nullable().optional(),
      alt: z.string().max(1024).nullable().optional(),
    }),
  }),
  z.object({ kind: z.literal("remove-element"), id: ElementId }),
  z.object({ kind: z.literal("duplicate-element"), id: ElementId }),
  /** Swap the whole theme (tokens, theme CSS, fonts); the slides are untouched. */
  z.object({ kind: z.literal("set-theme"), theme: z.string().min(1).max(60) }),
  z.object({
    kind: z.literal("set-full-source"),
    source: z.string().min(1).max(25_000_000),
    /** What a text document's editor changed ("edited paragraph 2"): the version's note. */
    summary: z.string().max(400).optional(),
  }),
]);
export type HtmlPatch = z.infer<typeof HtmlPatchSchema>;

export const artifactsContract = {
  artifacts: {
    list: oc.input(botId).output(z.array(ArtifactSchema)),
    listSpace: oc
      .input(
        z.object({
          botId: Id.optional(),
          cursor: z.string().optional(),
          limit: z.number().int().min(1).max(60).optional(),
        }),
      )
      .output(
        z.object({
          items: z.array(ArtifactSchema.extend({ versionCount: z.number().int() })),
          nextCursor: z.string().nullable(),
        }),
      ),
    listVersions: oc.input(z.object({ familyId: Id })).output(z.array(ArtifactVersionSchema)),
    getById: oc.input(z.object({ artifactId: Id })).output(ArtifactWithContentSchema),
    /** The file's studio chat (any version id), made on first open; never wakes the host. With
     * it, the prelude: what made each version in Super Chat (empty when nothing did). */
    chat: oc.input(z.object({ artifactId: Id })).output(
      z.object({
        sessionId: Id,
        created: z.boolean(),
        prelude: z.array(StudioPreludeSchema),
      }),
    ),
    /** The file's studio is open (any version id): keeps its runner warm. Sent on open and
     * every few minutes while it stays open. */
    chatOpen: oc
      .input(z.object({ artifactId: Id }))
      .output(z.object({ sessionId: Id, warm: z.boolean() })),
    remove: oc.input(z.object({ artifactId: Id })).output(z.object({ ok: z.literal(true) })),
    /** Makes this earlier version (its id) the newest again, as a new `restore` version. */
    restore: oc.input(z.object({ artifactId: Id })).output(ArtifactSchema),
    /** Hand edits of an HTML file as source patches, or a text document's whole new text (one
     * `set-full-source`): a new `manual` version. CONFLICT with
     * `data.code` `artifact_stale` when `baseVersion` is no longer the newest, `conflict` when the
     * engine cannot apply a patch exactly (ask Superchat instead). */
    edit: oc
      .input(
        z.object({
          artifactId: Id,
          baseVersion: z.number().int().min(1),
          patches: z.array(HtmlPatchSchema).min(1).max(200),
        }),
      )
      .output(ArtifactSchema),
  },
};
