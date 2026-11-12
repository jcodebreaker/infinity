import { eventIterator, oc } from "@orpc/contract";
import * as z from "zod";
import { ATTACHMENT_MAX_COUNT, WorkspaceAttachmentSchema } from "../attachments.js";
import { ThreadMessagePageSchema } from "../domain.js";
import { ThreadMessageSchema } from "../events.js";
import { Id } from "../ids.js";

/** A Round (ADR 0029): one person's message through settlement, as the engine's record folds it.
 * Times are epoch ms on the engine's clock. `status`: `idle`, `running`, `done`, `failed` or `stopped`. */
export const RoundSummarySchema = z.object({
  /** The id of the person's message that opened it. */
  roundId: z.string(),
  status: z.string(),
  startedAt: z.number().nullable(),
  /** When it settled; null while it runs. */
  settledAt: z.number().nullable(),
  /** When it delivered its answer (see `RoundView.deliveredAt`); null until then. */
  deliveredAt: z.number().nullable(),
  /** The running tool's digest, else its name. */
  live: z.string().nullable(),
  helpers: z.number(),
  files: z.number(),
  steps: z.number(),
});
export type RoundSummary = z.infer<typeof RoundSummarySchema>;

/** Who acted in a Round: the Assistant (`helperId` null) or one of its Helpers. */
const RoundActor = { actor: z.string().nullable(), helperId: z.string().nullable() };

/** One Round in full (`GET /v1/sessions/{id}/rounds/{roundId}`): every word is the harness's or
 * the record's; `stopLine` is the one line the engine writes, already in the transcript. */
export const RoundViewSchema = z.object({
  roundId: z.string(),
  status: z.string(),
  startedAt: z.number().nullable(),
  settledAt: z.number().nullable(),
  elapsedMs: z.number(),
  /** When the Round delivered its answer (a terminal card, the chips or a question, with no
   * Helper of it running); it may still run until its turn ends, but its strip reads settled. */
  deliveredAt: z.number().nullable(),
  stopLine: z.string().nullable(),
  /** The tool running now. */
  now: z
    .object({ ...RoundActor, name: z.string().nullable(), digest: z.string().nullable() })
    .nullable(),
  /** Finished tool calls, in order. */
  trail: z.array(
    z.object({
      ...RoundActor,
      at: z.number(),
      name: z.string().nullable(),
      digest: z.string().nullable(),
      ok: z.boolean(),
    }),
  ),
  /** Helpers in the order they started. */
  helpers: z.array(
    z.object({
      id: z.string(),
      title: z.string().nullable(),
      folder: z.string().nullable(),
      /** `running`, `done`, `failed` or `stopped`. */
      status: z.string(),
      startedAt: z.number(),
      finishedAt: z.number().nullable(),
    }),
  ),
  /** Files saved, a re-save replacing the earlier one. */
  files: z.array(
    z.object({
      artifactId: z.string().nullable(),
      name: z.string().nullable(),
      version: z.number().nullable(),
      helperId: z.string().nullable(),
    }),
  ),
  /** The model's checklist, in its own words; it drives nothing. */
  checklist: z.array(
    z.object({
      id: z.string().nullable(),
      subject: z.string().nullable(),
      activeForm: z.string().nullable(),
      /** `pending`, `in_progress` or `completed`. */
      status: z.string(),
    }),
  ),
});
export type RoundView = z.infer<typeof RoundViewSchema>;

/** One event of a Muse's live family stream (the Conversation and its Helpers).
 * Ids only: the client refetches what changed. `open` is local to the relay: the stream just
 * (re)connected, so anything may have been missed. */
export const FamilyEventSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("open") }),
  z.object({ type: z.literal("messageDone"), chatId: z.string(), itemId: z.string() }),
  /** A message the person sent during a turn started or stopped waiting behind it; its
   * transcript message says which (`delivered: "queued"` while it waits). */
  z.object({ type: z.literal("messageDelivery"), chatId: z.string(), itemId: z.string() }),
  /** A turn in this chat ended (completed, failed, incomplete or cancelled); a failure's
   * error note is already in the transcript. */
  z.object({ type: z.literal("turnDone"), chatId: z.string(), status: z.string() }),
  /** The chat was cleared; its transcript now starts at the reset. */
  z.object({ type: z.literal("chatReset"), chatId: z.string(), itemId: z.string() }),
  z.object({ type: z.literal("chatsChanged") }),
  z.object({ type: z.literal("activitiesChanged") }),
  /** A version of a file was saved (`groupId`: the file, `Artifact.groupId`). */
  z.object({ type: z.literal("artifactsChanged"), groupId: z.string() }),
  /** A Round's record grew: its summary; `chats.round` reads it in full. */
  RoundSummarySchema.extend({ type: z.literal("roundUpdated") }),
  /** A chat of the family (a file's studio, a Helper) changed status: `running`, `waiting`,
   * `idle` or `failed`; `blockedOn` is the engine's words while its running turn waits for a slot
   * ("Finishing another file first", ADR 0024). Every family chat's, so no page needs a chat's
   * own stream to follow its status. */
  z.object({
    type: z.literal("chatStatus"),
    chatId: z.string(),
    status: z.string(),
    blockedOn: z.string().nullable(),
  }),
  /** A decision waiting on the person was asked or answered: `asks.list` reads them. */
  z.object({ type: z.literal("asksChanged") }),
  z.object({ type: z.literal("heartbeat") }),
]);
export type FamilyEvent = z.infer<typeof FamilyEventSchema>;

/** The reply a chat's running turn is writing, as it streams: `text` appends to it (after a
 * (re)connect, the first `text` is everything written so far), `textReset` says it was stored
 * as a message or the turn ended. Display only; whether the turn runs is the transcript's. */
export const ReplyEventSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("text"), delta: z.string() }),
  z.object({ kind: z.literal("textReset") }),
]);
export type ReplyEvent = z.infer<typeof ReplyEventSchema>;

export const chatsContract = {
  chats: {
    /** The Conversation (no `chatId`) or one of its Helpers, as typed blocks.
     * Newest page by default; pass `before` (an `olderItemCursor` from a previous page) to page
     * further back. */
    transcript: oc
      .input(
        z.object({
          botId: Id,
          chatId: Id.optional(),
          before: z.string().min(1).optional(),
          /** Read the history older than the chat's latest reset instead. */
          beforeReset: z.boolean().optional(),
        }),
      )
      .output(ThreadMessagePageSchema),
    /** The Conversation's latest Rounds, newest first. */
    rounds: oc
      .input(z.object({ botId: Id }))
      .output(z.object({ rounds: z.array(RoundSummarySchema) })),
    /** One Round of the Conversation in full (NOT_FOUND for a message that opened none). */
    round: oc.input(z.object({ botId: Id, roundId: Id })).output(RoundViewSchema),
    /** The person has the Conversation open: marks it read (and clears its unread dot). */
    markRead: oc.input(z.object({ botId: Id })).output(z.object({ ok: z.literal(true) })),
    /** Clears the Muse's Conversation (CONFLICT while a turn is running). */
    reset: oc.input(z.object({ botId: Id })).output(z.object({ ok: z.literal(true) })),
    /** The Muse's live family stream: ids only, refetch what changed. */
    watch: oc.input(z.object({ botId: Id })).output(eventIterator(FamilyEventSchema)),
    /** The reply being written in the Conversation (`chatId` is its id) or one of the Muse's
     * chats, as it streams. */
    watchReply: oc
      .input(z.object({ botId: Id, chatId: Id }))
      .output(eventIterator(ReplyEventSchema)),
    /** Sends the person's message to the Muse's Conversation. Files are already in the
     * workspace (`files.uploadAttachment`); the engine queues or steers a message sent while a
     * turn runs. */
    send: oc
      .input(
        z
          .object({
            botId: Id,
            /** A file's studio chat (`artifacts.chat`) instead of the Conversation (ADR 0024). */
            chatId: Id.optional(),
            text: z.string().optional(),
            attachments: z.array(WorkspaceAttachmentSchema).max(ATTACHMENT_MAX_COUNT).optional(),
            /** Workspace folders (relative, as the Files browser lists them) the message points at. */
            folders: z.array(z.string().max(1024)).max(5).optional(),
            /** What is beside the chat: the artifact open in the panel is a hint for the Muse
             * and the message's `open` link, never a hard scope. */
            context: z
              .object({
                openArtifact: z
                  .object({ artifactId: Id, version: z.number().int().min(1) })
                  .optional(),
                /** The message answers an ask card: which card and option (the text is its label). */
                cardAnswer: z
                  .object({
                    cardId: z.string().min(1).max(200),
                    optionId: z.string().min(1).max(64),
                    label: z.string().min(1).max(200),
                  })
                  .optional(),
              })
              .optional(),
          })
          .refine(
            (input) =>
              (input.text?.trim() ?? "") ||
              (input.attachments?.length ?? 0) > 0 ||
              (input.folders?.length ?? 0) > 0,
            "Write a message or attach something",
          ),
      )
      .output(z.object({ ok: z.literal(true) })),
    /** The person picks which artifact their message is about (`null`: none). The link becomes
     * `chosen` and a running turn is steered; answers with the updated message. */
    setMessageArtifact: oc
      .input(z.object({ botId: Id, itemId: Id, artifactId: Id.nullable() }))
      .output(ThreadMessageSchema),
    /** Stops the turn running in the Muse's Conversation and its running Helpers; messages waiting
     * behind it are cancelled. `delivered`: whether the stop landed. */
    interrupt: oc
      .input(
        z.object({
          botId: Id,
          /** A file's studio chat instead of the Conversation. */
          chatId: Id.optional(),
        }),
      )
      .output(z.object({ delivered: z.boolean() })),
    /** Holds the newest reply's Helpers at their next tool boundary (ADR 0027); the
     * Conversation's own turn is never paused. A Helper left paused for 30 minutes is stopped.
     * `helperIds` narrows it (default: every live Helper); answers with the Helpers paused. */
    pauseHelpers: oc
      .input(z.object({ botId: Id, helperIds: z.array(Id).optional() }))
      .output(z.object({ paused: z.array(z.string()) })),
    /** Lets paused Helpers go on (default: every paused one); answers with those resumed. */
    resumeHelpers: oc
      .input(z.object({ botId: Id, helperIds: z.array(Id).optional() }))
      .output(z.object({ resumed: z.array(z.string()) })),
    /** Where the Conversation works: its Project (its working directory is a Project folder) or
     * null, the folder relative to the workspace (null: the whole workspace) and whether the
     * person chose it. */
    project: oc.input(z.object({ botId: Id })).output(
      z.object({
        project: z.object({ slug: z.string(), name: z.string() }).nullable(),
        folder: z.string().nullable().optional(),
        chosen: z.boolean().optional(),
      }),
    ),
    /** The person moves the Conversation to a workspace folder, relative as the Files browser
     * lists it; `null` is the whole workspace and clears their choice. The Muse keeps a folder
     * the person chose and is told of the move once. */
    setFolder: oc
      .input(z.object({ botId: Id, folder: z.string().max(1024).nullable() }))
      .output(z.object({ ok: z.literal(true) })),
  },
};
