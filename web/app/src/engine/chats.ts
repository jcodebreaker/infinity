import { ORPCError } from "@orpc/server";
import type {
  OmnigentMessageContext,
  OmnigentRoundSummary,
  OmnigentRoundView,
} from "@superchat/adapters/omnigent/browser";
import {
  confineWorkspacePath,
  getOmnigentRound,
  getOmnigentTranscript,
  getOmnigentWorkingPlace,
  getOmnigentWorkspaceRoot,
  interruptOmnigentSession,
  listOmnigentRounds,
  mapReplyFrame,
  mapTranscriptMessage,
  mapTranscriptPage,
  markOmnigentRead,
  moveOmnigentSessionForPerson,
  OmnigentApiError,
  postOmnigentMessage,
  resetOmnigentSession,
  resolveChatOwnership,
  setOmnigentHelpersPaused,
  setOmnigentMessageArtifact,
  streamOmnigentFamily,
  streamOmnigentSession,
} from "@superchat/adapters/omnigent/browser";
import type { FamilyEvent, RoundSummary, RoundView } from "@superchat/contracts";
import { promptTextForWorkspaceAttachments } from "@superchat/core";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";
import { validateWorkspaceAttachments, validateWorkspaceFolders } from "./workspace-attachments";

/** Messages per transcript page. */
const CHATS_PAGE_SIZE = 50;

/** The Muse's Conversation session. The web's `botId` is that session's id, so a different one
 * names a Muse this person does not have. */
async function requireMuse(botId: string): Promise<string> {
  const sessionId = await museSessionId();
  if (botId !== sessionId) throw new ORPCError("NOT_FOUND", { message: "Muse not found" });
  return sessionId;
}

/** A Helper of the Muse `botId` names; `NOT_FOUND` for anything else, so a chat's existence
 * never leaks. */
async function requireOwnHelper(botId: string, chatId: string): Promise<void> {
  const sessionId = await museSessionId();
  const ownership = await resolveChatOwnership(
    engineClient(),
    NO_EMAIL,
    [{ botId: sessionId, omnigentSessionId: sessionId }],
    chatId,
  );
  if (!ownership || ownership.botId !== botId) {
    throw new ORPCError("NOT_FOUND", { message: "Chat not found" });
  }
}

/** The Muse's Conversation or one of its Helpers; `NOT_FOUND` for anything else. */
async function requireOwnChat(botId: string, chatId: string): Promise<void> {
  if (chatId === (await requireMuse(botId))) return;
  await requireOwnHelper(botId, chatId);
}

/** Where a message goes: the Conversation, or one of the Muse's chats a person writes in (a
 * file's studio chat, `artifacts.chat`); `NOT_FOUND` for any other chat. */
async function targetChat(input: { botId: string; chatId?: string }): Promise<string> {
  if (!input.chatId) return requireMuse(input.botId);
  await requireOwnHelper(input.botId, input.chatId);
  return input.chatId;
}

/** A workspace folder (relative, as the Files browser lists it; `""` is the whole workspace) as
 * an absolute path under the Muse's workspace root. */
async function folderWorkspace(
  superSessionId: string,
  folder: string,
): Promise<{ relative: string; path: string }> {
  let relative: string;
  try {
    relative = confineWorkspacePath(folder);
  } catch {
    throw new ORPCError("BAD_REQUEST", { message: "That folder is outside the workspace." });
  }
  const root = await getOmnigentWorkspaceRoot(engineClient(), NO_EMAIL, superSessionId);
  if (!root) throw new ORPCError("NOT_FOUND", { message: "No workspace yet" });
  return { relative, path: relative ? `${root}/${relative}` : root };
}

/** The person moves the Conversation to a folder; `null` is the whole workspace. */
async function setChatFolder(input: {
  botId: string;
  folder: string | null;
}): Promise<{ ok: true }> {
  const sessionId = await requireMuse(input.botId);
  const workspace = await folderWorkspace(sessionId, input.folder ?? "");
  try {
    await moveOmnigentSessionForPerson(
      engineClient(),
      NO_EMAIL,
      sessionId,
      workspace.path,
      input.folder !== null && workspace.relative !== "",
    );
  } catch (error) {
    if (error instanceof OmnigentApiError && error.code === "invalid_input") {
      throw new ORPCError("BAD_REQUEST", { message: "Superchat can't work in that folder." });
    }
    throw error;
  }
  return { ok: true };
}

/** The Conversation (no `chatId`) or one of its Helpers, as the engine's transcript (ADR 0009).
 * A Helper's chat is read-only. Newest page by default; `before` (an earlier page's
 * `olderItemCursor`) pages further back. */
export async function getChatTranscript(input: {
  botId: string;
  chatId?: string;
  before?: string;
  beforeReset?: boolean;
}) {
  let sessionId: string;
  if (input.chatId) {
    await requireOwnHelper(input.botId, input.chatId);
    sessionId = input.chatId;
  } else {
    sessionId = await requireMuse(input.botId);
  }
  const page = await onSuperChat(
    getOmnigentTranscript(engineClient(), NO_EMAIL, sessionId, {
      limit: CHATS_PAGE_SIZE,
      before: input.before,
      beforeReset: input.beforeReset,
    }),
  );
  const mapped = mapTranscriptPage(sessionId, page);
  return input.chatId ? { ...mapped, readOnly: true } : mapped;
}

/** Epoch seconds (the engine's) as epoch ms. */
const ms = (seconds: number) => Math.round(seconds * 1000);

/** A Round's summary, from the engine's list or its `round.updated` frame. */
export function roundSummary(round: OmnigentRoundSummary): RoundSummary {
  return {
    roundId: round.round_id,
    status: round.status,
    startedAt: round.started_at === null ? null : ms(round.started_at),
    settledAt: round.settled_at == null ? null : ms(round.settled_at),
    deliveredAt: round.delivered_at == null ? null : ms(round.delivered_at),
    live: round.live,
    helpers: round.helpers,
    files: round.files,
    steps: round.steps,
  };
}

/** A Round in full, as the web reads it. */
export function roundView(round: OmnigentRoundView): RoundView {
  return {
    roundId: round.round_id,
    status: round.status,
    startedAt: round.started_at === null ? null : ms(round.started_at),
    settledAt: round.settled_at === null ? null : ms(round.settled_at),
    elapsedMs: ms(round.elapsed),
    deliveredAt: round.delivered_at == null ? null : ms(round.delivered_at),
    stopLine: round.stop_line,
    now: round.now && {
      actor: round.now.actor,
      helperId: round.now.helper_id,
      name: round.now.name,
      digest: round.now.digest,
    },
    trail: round.trail.map((step) => ({
      actor: step.actor,
      helperId: step.helper_id,
      at: ms(step.t),
      name: step.name,
      digest: step.digest,
      ok: step.ok,
    })),
    helpers: Object.values(round.helpers)
      .map((helper) => ({
        id: helper.id,
        title: helper.title,
        folder: helper.folder,
        status: helper.status,
        startedAt: ms(helper.started_at),
        finishedAt: helper.finished_at === null ? null : ms(helper.finished_at),
      }))
      .sort((a, b) => a.startedAt - b.startedAt),
    files: round.files.map((file) => ({
      artifactId: file.artifact_id,
      name: file.name,
      version: file.version,
      helperId: file.helper_id,
    })),
    checklist: round.checklist.map((item) => ({
      id: item.id,
      subject: item.subject,
      activeForm: item.active_form,
      status: item.status,
    })),
  };
}

/** The Muse's family stream (Conversation and Helpers), relayed as ids only so the
 * client refetches what changed. Ends when the engine's stream does; the client reconnects and
 * refetches. */
export async function* watchFamily(
  input: { botId: string },
  signal: AbortSignal | undefined,
): AsyncGenerator<FamilyEvent> {
  const superSessionId = await requireMuse(input.botId);
  yield { type: "open" };
  try {
    for await (const frame of streamOmnigentFamily(
      engineClient(),
      NO_EMAIL,
      superSessionId,
      signal,
    )) {
      if (frame.type === "message.done") {
        yield { type: "messageDone", chatId: frame.chat_id, itemId: frame.item_id };
      } else if (frame.type === "message.delivery") {
        yield { type: "messageDelivery", chatId: frame.chat_id, itemId: frame.item_id };
      } else if (frame.type === "chat.reset") {
        yield { type: "chatReset", chatId: frame.chat_id, itemId: frame.item_id };
      } else if (frame.type === "turn.done") {
        yield { type: "turnDone", chatId: frame.chat_id, status: frame.status };
      } else if (frame.type === "chats.changed") yield { type: "chatsChanged" };
      else if (frame.type === "activities.changed") yield { type: "activitiesChanged" };
      else if (frame.type === "artifacts.changed") {
        yield { type: "artifactsChanged", groupId: frame.group_id };
      } else if (frame.type === "round.updated") {
        yield { type: "roundUpdated", ...roundSummary(frame) };
      } else if (frame.type === "chat.status") {
        yield {
          type: "chatStatus",
          chatId: frame.chat_id,
          status: frame.status,
          blockedOn: frame.blocked_on || null,
        };
      } else if (frame.type === "asks.changed") yield { type: "asksChanged" };
      else yield { type: "heartbeat" };
    }
  } catch (error) {
    // A closed tab aborts the fetch; that is the normal end of a watch, not a failure.
    if (!signal?.aborted) throw error;
  }
}

/**
 * The person's message to the Muse's Conversation. Attached
 * files are already in the workspace: the message carries one reference line each (the same lines
 * the Conversation shows back as chips); the engine hands an image to the model as an image. The
 * engine owns delivery: a message sent while a turn runs is steered into it or queued behind it.
 */
export async function sendToChat(input: {
  botId: string;
  chatId?: string;
  text?: string;
  attachments?: Parameters<typeof validateWorkspaceAttachments>[0];
  folders?: string[];
  context?: OmnigentMessageContext;
}): Promise<{ ok: true }> {
  const sessionId = await targetChat(input);
  const text = promptTextForWorkspaceAttachments(
    input.text,
    validateWorkspaceAttachments(input.attachments ?? []),
    validateWorkspaceFolders(input.folders ?? []),
  );
  if (!text) throw new ORPCError("BAD_REQUEST", { message: "Nothing to send." });
  const client = engineClient();
  const context = input.context;
  await onSuperChat(
    context?.openArtifact || context?.cardAnswer
      ? postOmnigentMessage(client, NO_EMAIL, sessionId, text, context)
      : postOmnigentMessage(client, NO_EMAIL, sessionId, text),
  );
  return { ok: true as const };
}

/** The person picks which artifact their message is about (`null`: none); answers with the
 * message as the transcript will show it. */
export async function setMessageArtifact(input: {
  botId: string;
  itemId: string;
  artifactId: string | null;
}) {
  const sessionId = await requireMuse(input.botId);
  let message: Awaited<ReturnType<typeof setOmnigentMessageArtifact>>;
  try {
    message = await onSuperChat(
      setOmnigentMessageArtifact(
        engineClient(),
        NO_EMAIL,
        sessionId,
        input.itemId,
        input.artifactId,
      ),
    );
  } catch (error) {
    if (error instanceof OmnigentApiError && error.code === "not_found") {
      throw new ORPCError("NOT_FOUND", { message: "Message not found" });
    }
    throw error;
  }
  return mapTranscriptMessage(sessionId, message);
}

export const chatsRouter = {
  chats: {
    transcript: os.chats.transcript.handler(({ input }) => getChatTranscript(input)),
    rounds: os.chats.rounds.handler(async ({ input }) => {
      const sessionId = await requireMuse(input.botId);
      const rounds = await onSuperChat(listOmnigentRounds(engineClient(), NO_EMAIL, sessionId));
      return { rounds: rounds.map(roundSummary) };
    }),
    round: os.chats.round.handler(async ({ input }) => {
      const sessionId = await requireMuse(input.botId);
      return roundView(
        await onSuperChat(getOmnigentRound(engineClient(), NO_EMAIL, sessionId, input.roundId)),
      );
    }),
    // The person has the Conversation open; moves their engine read baseline so its unread dot
    // clears.
    markRead: os.chats.markRead.handler(async ({ input }) => {
      const sessionId = await requireMuse(input.botId);
      await onSuperChat(markOmnigentRead(engineClient(), NO_EMAIL, sessionId));
      return { ok: true as const };
    }),
    // Clears the Muse's Conversation. The engine refuses while a turn is running; every client
    // then refetches on the family stream's `chat.reset`.
    reset: os.chats.reset.handler(async ({ input }) => {
      const sessionId = await requireMuse(input.botId);
      try {
        await onSuperChat(resetOmnigentSession(engineClient(), NO_EMAIL, sessionId));
      } catch (error) {
        if (error instanceof OmnigentApiError && error.code === "conflict") {
          throw new ORPCError("CONFLICT", { message: "Wait for Superchat to finish, then clear." });
        }
        throw error;
      }
      return { ok: true as const };
    }),
    watchReply: os.chats.watchReply.handler(async function* ({ input, signal }) {
      await requireOwnChat(input.botId, input.chatId);
      try {
        for await (const frame of streamOmnigentSession(
          engineClient(),
          NO_EMAIL,
          input.chatId,
          signal,
        )) {
          const event = mapReplyFrame(frame);
          if (event) yield event;
        }
      } catch (error) {
        if (!signal?.aborted) throw error;
      }
    }),
    watch: os.chats.watch.handler(async function* ({ input, signal }) {
      yield* watchFamily(input, signal);
    }),
    send: os.chats.send.handler(({ input }) => sendToChat(input)),
    setMessageArtifact: os.chats.setMessageArtifact.handler(({ input }) =>
      setMessageArtifact(input),
    ),
    interrupt: os.chats.interrupt.handler(async ({ input }) => {
      const sessionId = await targetChat(input);
      return onSuperChat(interruptOmnigentSession(engineClient(), NO_EMAIL, sessionId));
    }),
    pauseHelpers: os.chats.pauseHelpers.handler(async ({ input }) => {
      const sessionId = await requireMuse(input.botId);
      const paused = await onSuperChat(
        setOmnigentHelpersPaused(engineClient(), NO_EMAIL, sessionId, true, input.helperIds),
      );
      return { paused };
    }),
    resumeHelpers: os.chats.resumeHelpers.handler(async ({ input }) => {
      const sessionId = await requireMuse(input.botId);
      const resumed = await onSuperChat(
        setOmnigentHelpersPaused(engineClient(), NO_EMAIL, sessionId, false, input.helperIds),
      );
      return { resumed };
    }),
    // The Project the Conversation has open (ADR 0008), as the engine reports it on the session.
    project: os.chats.project.handler(async ({ input }) => {
      const sessionId = await requireMuse(input.botId);
      return onSuperChat(getOmnigentWorkingPlace(engineClient(), NO_EMAIL, sessionId));
    }),
    setFolder: os.chats.setFolder.handler(({ input }) => onSuperChat(setChatFolder(input))),
  },
};
