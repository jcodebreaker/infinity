import * as z from "zod";
import { ThreadMessageSchema } from "./events.js";
import { Id, RunStatus } from "./ids.js";

export const AvatarStyleSchema = z.enum(["robot", "organic"]);
export type AvatarStyle = z.infer<typeof AvatarStyleSchema>;

export const BotSchema = z.object({
  /** The Muse's engine session id. */
  id: Id,
  name: z.string(),
  title: z.string(),
  description: z.string(),
  color: z.string(),
  unread: z.boolean(),
  preview: z.string(),
  status: z.string(),
  updatedAt: z.string(),
  createdAt: z.string(),
});
export type Bot = z.infer<typeof BotSchema>;

/** Selected-text excerpt carried by a reply; capped so a quote stays a quote. */
export const REPLY_QUOTE_MAX_LENGTH = 2_000;

/** Cap an excerpt at the quote limit without splitting a surrogate pair. */
export function truncateReplyQuote(value: string): string {
  const truncated = value.slice(0, REPLY_QUOTE_MAX_LENGTH);
  const last = truncated.charCodeAt(truncated.length - 1);
  return last >= 0xd800 && last <= 0xdbff ? truncated.slice(0, -1) : truncated;
}

export const ActionApprovalRuleSchema = z.object({
  id: Id,
  effect: z.enum(["always_allow", "require_approval"]),
  matchKind: z.enum(["tool", "connector", "category"]),
  matchValue: z.string(),
  createdAt: z.string(),
});
export type ActionApprovalRule = z.infer<typeof ActionApprovalRuleSchema>;

export const HostStatusSchema = z.object({
  botId: Id,
  state: z.enum(["stopped", "booting", "running", "suspended", "error"]),
  /** The Conversation's runner is connected (Files and tools can reach it); absent when unknown. */
  runnerReady: z.boolean().optional(),
  /** Where the host's start-up is, while the engine is launching it or it failed; absent
   * once it is up. Drives "Starting your machine…" and the failure note. */
  launch: z
    .object({ stage: z.enum(["provisioning", "cloning", "starting", "connecting", "failed"]) })
    .optional(),
});
export type HostStatus = z.infer<typeof HostStatusSchema>;

export const RunSchema = z.object({
  id: Id,
  botId: Id,
  threadId: Id,
  taskId: Id,
  status: RunStatus,
  trigger: z.enum([
    "user",
    "routine",
    "resume",
    "follow_up",
    "reaction",
    "spawn",
    "webhook",
    "messaging",
    "cloud_agent",
    "created",
    /** Super Chat slice A1 (docs/architecture.md): "the Muse speaks first" — an assistant
     * message the Super Chat produced on its own (a Helper's Result waking it), copied into
     * Superchat's thread by the mirror job (web/packages/adapters/src/omnigent/mirror.ts) rather than a
     * person's message or the normal run path. */
    "omnigent_mirror",
  ]),
  routineId: Id.nullable(),
  modelProvider: z.string().nullable(),
  modelId: z.string().nullable(),
  error: z.string().nullable(),
  startedAt: z.string().nullable(),
  completedAt: z.string().nullable(),
  createdAt: z.string(),
});
export type Run = z.infer<typeof RunSchema>;

export const ThreadMessagePageSchema = z.object({
  threadId: Id,
  messages: z.array(ThreadMessageSchema),
  olderCursor: z.number().int().nonnegative().nullable(),
  /** Omnigent-backed pages only (`chats.transcript`, docs/architecture.md): the engine's
   * `older_cursor`, to page further back with `chats.transcript({chatId, before})`. A Superchat-native
   * thread page (goals.log) never sets this; `olderCursor`'s integer seq keeps meaning what it
   * always has there. `null`/omitted means there is no older page. */
  olderItemCursor: z.string().nullable().optional(),
  /** `chats.transcript` only: the chat is working on a reply right now. */
  running: z.boolean().optional(),
  /** `chats.transcript` only: the chat was cleared; the page starts at the reset, and older
   * history is read with `beforeReset`. */
  reset: z.object({ itemId: z.string(), createdAt: z.string() }).nullable().optional(),
  /** `chats.transcript` only: a Helper's chat, which the person reads but never writes to. */
  readOnly: z.boolean().optional(),
});
export type ThreadMessagePage = z.infer<typeof ThreadMessagePageSchema>;

export const MeSchema = z.object({
  /** The engine's identity for the person (their email under SSO, `local` on a local engine). */
  userId: Id,
  name: z.string(),
  isAdmin: z.boolean(),
  needsModel: z.boolean(),
  defaultProvider: z.string().nullable(),
  defaultModel: z.string().nullable(),
  avatarStyle: AvatarStyleSchema,
  // IANA zone name; drives quiet hours and the engine's scheduled runs.
  timezone: z.string(),
});
export type Me = z.infer<typeof MeSchema>;
