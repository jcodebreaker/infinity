import { useLingui } from "@lingui/react/macro";
import { ORPCError } from "@orpc/client";
import type { Bot, ThreadMessage, WorkspaceAttachment } from "@superchat/contracts";
import { ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_COUNT } from "@superchat/contracts";
import {
  attachmentsForThread,
  inferAttachmentMimeType,
  isIngestableAttachmentMimeType,
  promptTextForWorkspaceAttachments,
} from "@superchat/core";
import type { MutableRefObject } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CardAnswer } from "../../../components/cards/context";
import { notifyAsksChanged } from "../../../features/approvals";
import { requestBrowserNotificationPermission } from "../../../lib/browser-notifications";
import { newClientId } from "../../../lib/client-id";
import { quotedMessageText } from "../../../lib/museTranscript";
import { revokePendingAttachmentPreviews } from "../../../lib/pending-attachments";
import { rpc } from "../../../lib/rpc";
import { userFacingError } from "../../../lib/user-facing-error";
import { markFirstRunSeen } from "../intro";
import { latestModelFailure } from "./failureNotes";
import { followHostStart, pollingHostFeed, runWhenHostReady } from "./hostReady";
import type { PendingAttachment } from "./shared";
import { attachmentsBlockSend, readFileAsBase64 } from "./shared";
import type { useMuseTranscript } from "./useMuseTranscript";

/** The host is asleep and still waking: the upload could not land yet. */
function isHostStarting(error: unknown): boolean {
  return error instanceof ORPCError && error.code === "SERVICE_UNAVAILABLE";
}

/** The host is awake but did not finish reading the file in time (not "starting"). */
function isReadTimeout(error: unknown): boolean {
  return error instanceof ORPCError && error.code === "GATEWAY_TIMEOUT";
}

/** One quiet retry covers the host waking up; a second failure is shown, never silent. */
const SEND_RETRY_DELAY_MS = 2500;

/** The send on its way: Stop cancels what has not gone out yet (`controller`), and waits for a
 * post already made (`posting`), whose message the engine may have. */
type SendInFlight = { controller: AbortController; posting: Promise<unknown> | null };

/** The composer's side of the Conversation: attachments, replies, sending, stopping and
 * answering asks. The messages themselves are the engine's (`useMuseTranscript`). */
export function useComposerSend({
  active,
  activeBotId,
  userId,
  messages,
  transcript,
  botsRef,
  flushPendingBrowserNotifications,
}: {
  active: Bot | undefined;
  activeBotId: MutableRefObject<string | undefined>;
  userId: string | undefined;
  /** The Conversation as shown: the latest failed reply drives the composer's note. */
  messages: readonly ThreadMessage[];
  transcript: Pick<
    ReturnType<typeof useMuseTranscript>,
    "beginSend" | "failSend" | "stopWaiting" | "refresh"
  >;
  botsRef: MutableRefObject<Bot[]>;
  flushPendingBrowserNotifications: () => void;
}) {
  const { t } = useLingui();
  // What the steps that wait for a sleeping host read its status from.
  const hostFeed = useMemo(
    () =>
      pollingHostFeed(async () => {
        const botId = activeBotId.current;
        return botId ? rpc.host.status({ botId }) : null;
      }),
    [],
  );
  const { beginSend, failSend, stopWaiting, refresh } = transcript;
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const pendingRef = useRef<PendingAttachment[]>([]);
  pendingRef.current = pendingAttachments;
  // Workspace folders the next message points at (a pointer, nothing is uploaded), per Muse.
  const [pendingFolders, setPendingFolders] = useState<{ threadKey: string; path: string }[]>([]);
  const [replyTarget, setReplyTarget] = useState<ThreadMessage | null>(null);
  const [replyQuote, setReplyQuote] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  /** The last Stop did not land; cleared by the caller once nothing runs (`clearStopFailed`). */
  const [stopFailed, setStopFailed] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  // First-run "Try it" chips (pages/app/intro) fill the composer with the
  // example rather than sending it, so the person sees it before it goes out.
  const [composerSeed, setComposerSeed] = useState<string | null>(null);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  // Files already written to the workspace, by pending id: a retry after a failed send does not
  // upload them twice.
  const uploadedRef = useRef(new Map<string, WorkspaceAttachment>());
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dismissedFailureIds, setDismissedFailureIds] = useState<ReadonlySet<string>>(new Set());
  /** Bumped on every send so the transcript jumps to the bottom and follows the reply. */
  const [followSignal, setFollowSignal] = useState(0);
  const activePendingAttachments = useMemo(
    () => attachmentsForThread(pendingAttachments, active?.id),
    [active?.id, pendingAttachments],
  );
  const activePendingFolders = useMemo(
    () => pendingFolders.filter((folder) => folder.threadKey === active?.id).map((f) => f.path),
    [active?.id, pendingFolders],
  );
  const attachFolder = useCallback(
    (path: string) => {
      const threadKey = active?.id;
      if (!threadKey) return;
      setPendingFolders((current) =>
        current.some((folder) => folder.threadKey === threadKey && folder.path === path)
          ? current
          : [...current, { threadKey, path }],
      );
    },
    [active?.id],
  );
  const removeFolder = useCallback((path: string) => {
    setPendingFolders((current) => current.filter((folder) => folder.path !== path));
  }, []);
  const clearReply = useCallback(() => {
    setReplyTarget(null);
    setReplyQuote(null);
  }, []);
  const failure = useMemo(() => {
    const latest = sendError ? null : latestModelFailure(messages);
    return latest && !dismissedFailureIds.has(latest.id) ? latest : null;
  }, [dismissedFailureIds, messages, sendError]);
  const answerMessage = useCallback(
    async (message: ThreadMessage, text: string, username?: string) => {
      const botId = activeBotId.current;
      if (!botId) return;
      // Muse Asks (Proposals, blocked Tasks, skill offers) apply their own effect; asks.answer
      // routes each kind.
      await rpc.asks.answer({
        askId: message.id,
        runId: message.runId ?? "",
        answer: text,
        ...(username ? { username } : {}),
      });
      notifyAsksChanged(botId);
      refresh();
    },
    [refresh],
  );
  const setAttachmentState = useCallback(
    (id: string, status: PendingAttachment["status"], error?: string) =>
      setPendingAttachments((current) =>
        current.map((item) =>
          item.id === id
            ? { ...item, status, error: status === "error" ? error : undefined }
            : item,
        ),
      ),
    [],
  );
  // One AbortController per attachment still being prepared: removing the chip, leaving the chat
  // or closing the page cancels its wait for the host and its read.
  const controllersRef = useRef(new Map<string, AbortController>());
  // The send in flight: its upload is cancelled by Stop, or when the page goes.
  const inFlight = useRef<SendInFlight | null>(null);
  useEffect(() => () => inFlight.current?.controller.abort(), []);
  const abortAttachment = useCallback((id: string) => {
    controllersRef.current.get(id)?.abort();
    controllersRef.current.delete(id);
  }, []);
  useEffect(() => {
    const controllers = controllersRef.current;
    return () => {
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
    };
  }, []);
  /** An attachment is written to the host and read there as soon as it is picked, so its
   * text is ready before the message goes (uploading, reading, ready, or a calm error). A sleeping
   * host is waited for by its status (the chip says so), never by a retry timer. */
  const prepareAttachment = useCallback(
    async (attachment: PendingAttachment, botId: string) => {
      const mimeType = inferAttachmentMimeType(attachment.file.name, attachment.file.type);
      if (!mimeType) return;
      abortAttachment(attachment.id);
      const controller = new AbortController();
      controllersRef.current.set(attachment.id, controller);
      const { signal } = controller;
      let unfollow: (() => void) | undefined;
      try {
        let stored = uploadedRef.current.get(attachment.id);
        if (!stored) {
          setAttachmentState(attachment.id, "uploading");
          const contentBase64 = await readFileAsBase64(attachment.file);
          signal.throwIfAborted();
          stored = await runWhenHostReady(
            () =>
              rpc.files.uploadAttachment(
                { botId, name: attachment.file.name, mimeType, contentBase64 },
                { signal },
              ),
            {
              isStarting: isHostStarting,
              onStarting: () => setAttachmentState(attachment.id, "starting"),
              feed: hostFeed,
              signal,
            },
          );
          uploadedRef.current.set(attachment.id, stored);
        }
        if (isIngestableAttachmentMimeType(mimeType)) {
          const path = stored.path;
          setAttachmentState(attachment.id, "reading");
          // One call: the engine wakes the host and waits for the read. The chip follows the
          // host's own status while it does.
          unfollow = followHostStart(hostFeed, {
            onStarting: () => setAttachmentState(attachment.id, "starting"),
            onUp: () => setAttachmentState(attachment.id, "reading"),
          });
          await rpc.files.ingestAttachment({ botId, path }, { signal });
          unfollow();
          unfollow = undefined;
        }
        setAttachmentState(attachment.id, "ready");
      } catch (error) {
        if (signal.aborted) return; // the chip is gone, or the person left: nothing to report
        setAttachmentState(
          attachment.id,
          "error",
          isHostStarting(error)
            ? t`Your machine is starting. Try again in a moment.`
            : isReadTimeout(error)
              ? t`Reading ${attachment.file.name} is taking too long. Try again in a moment.`
              : userFacingError(error, t`Couldn't read ${attachment.file.name}`),
        );
      } finally {
        unfollow?.();
        if (controllersRef.current.get(attachment.id) === controller) {
          controllersRef.current.delete(attachment.id);
        }
      }
    },
    [abortAttachment, hostFeed, setAttachmentState, t],
  );
  const retryAttachment = useCallback(
    (attachment: PendingAttachment) => {
      const botId = activeBotId.current;
      if (botId) void prepareAttachment(attachment, botId);
    },
    [prepareAttachment],
  );
  const onAttachmentPick = useCallback(
    async (files: FileList | null) => {
      const threadKey = activeBotId.current;
      if (!threadKey || !files?.length) return;
      const existing = attachmentsForThread(pendingAttachments, threadKey);
      const next: PendingAttachment[] = [];
      const skipped: string[] = [];
      for (const file of Array.from(files)) {
        if (existing.length + next.length >= ATTACHMENT_MAX_COUNT) {
          skipped.push(t`${file.name} (max ${ATTACHMENT_MAX_COUNT} attachments)`);
          continue;
        }
        if (file.size > ATTACHMENT_MAX_BYTES) {
          skipped.push(t`${file.name} (over 10 MiB)`);
          continue;
        }
        const mimeType = inferAttachmentMimeType(file.name, file.type);
        if (!mimeType) {
          skipped.push(file.name);
          continue;
        }
        next.push({
          id: newClientId(),
          threadKey,
          file,
          previewUrl: mimeType.startsWith("image/") ? URL.createObjectURL(file) : undefined,
          status: "uploading" as const,
        });
      }
      if (next.length) setPendingAttachments((current) => [...current, ...next]);
      for (const item of next) void prepareAttachment(item, threadKey);
      setAttachmentNotice(skipped.length ? t`Skipped ${skipped.join(", ")}` : null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    },
    [pendingAttachments, prepareAttachment, t],
  );
  const removeAttachment = useCallback(
    (attachment: PendingAttachment) => {
      abortAttachment(attachment.id);
      revokePendingAttachmentPreviews([attachment]);
      uploadedRef.current.delete(attachment.id);
      setPendingAttachments((current) => current.filter((item) => item.id !== attachment.id));
    },
    [abortAttachment],
  );
  /** Resolves `true` only when the message (or its routine action) really went out; every other
   * path resolves `false` so the composer gives the person's words back. */
  const sendMessage = useCallback(
    async (text: string, cardAnswer?: CardAnswer): Promise<boolean> => {
      const botTarget = activeBotId.current;
      if (!botTarget || sending) return false;
      setFollowSignal((current) => current + 1);
      const attachments = attachmentsForThread(pendingAttachments, botTarget);
      const folders = pendingFolders.filter((f) => f.threadKey === botTarget).map((f) => f.path);
      // A file still being read (or one that failed) holds the message back; Send is disabled.
      if (attachmentsBlockSend(attachments)) return false;
      const trimmed = text.trim();
      if (!trimmed && attachments.length === 0 && folders.length === 0) return false;
      // The first real message closes the first-run welcome for good (pages/app/intro),
      // wherever it was sent from — the composer directly, or a welcome card's "Try it".
      markFirstRunSeen(userId, "welcome");
      const permissionRequest = requestBrowserNotificationPermission();
      if (permissionRequest) void permissionRequest.then(flushPendingBrowserNotifications);
      setSending(true);
      setSendError(null);
      setStopFailed(false);
      const flight: SendInFlight = { controller: new AbortController(), posting: null };
      inFlight.current = flight;
      const { signal } = flight.controller;
      const sentFiles = () => {
        clearReply();
        revokePendingAttachmentPreviews(attachments);
        for (const sentAttachment of attachments) uploadedRef.current.delete(sentAttachment.id);
        setPendingAttachments((current) =>
          current.filter((attachment) => attachment.threadKey !== botTarget),
        );
        setPendingFolders((current) => current.filter((f) => f.threadKey !== botTarget));
        setAttachmentNotice(null);
      };
      let showing = false;
      try {
        const workspaceAttachments: WorkspaceAttachment[] = [];
        let uploaded = 0;
        for (const pending of attachments) {
          const mimeType = inferAttachmentMimeType(pending.file.name, pending.file.type);
          if (!mimeType) {
            throw new Error(t`Unsupported file type: ${pending.file.name}`);
          }
          let stored = uploadedRef.current.get(pending.id);
          if (!stored) {
            setUploadStatus(t`Uploading ${uploaded + 1} of ${attachments.length}`);
            const contentBase64 = await readFileAsBase64(pending.file);
            const progress = t`Uploading ${uploaded + 1} of ${attachments.length}`;
            stored = await runWhenHostReady(
              () =>
                rpc.files.uploadAttachment({
                  botId: botTarget,
                  name: pending.file.name,
                  mimeType,
                  contentBase64,
                }),
              {
                isStarting: isHostStarting,
                onStarting: () => setUploadStatus(t`Starting your machine…`),
                feed: hostFeed,
                signal,
              },
            );
            setUploadStatus(progress);
            uploadedRef.current.set(pending.id, stored);
          }
          uploaded += 1;
          workspaceAttachments.push(stored);
        }
        setUploadStatus(null);
        // A quote travels as text the engine records and the model reads, not as a reply link.
        const quoted = replyQuote ? quotedMessageText(replyQuote, trimmed) : trimmed;
        // The message appears at once, as the engine will record it (file lines become chips).
        const shownText = promptTextForWorkspaceAttachments(quoted, workspaceAttachments, folders);
        beginSend(shownText);
        showing = true;
        // A post once made is never cancelled: the engine may already have the message, and a
        // Stop waits for it instead.
        const post = () => {
          flight.posting = rpc.chats.send({
            botId: botTarget,
            text: quoted || undefined,
            attachments: workspaceAttachments.length ? workspaceAttachments : undefined,
            folders: folders.length ? folders : undefined,
            ...(cardAnswer ? { context: { cardAnswer } } : {}),
          });
          return flight.posting;
        };
        signal.throwIfAborted();
        try {
          await post();
        } catch {
          await new Promise<void>((resolve) => window.setTimeout(resolve, SEND_RETRY_DELAY_MS));
          signal.throwIfAborted();
          await post();
        }
        sentFiles();
        refresh();
        return true;
      } catch (error) {
        if (showing) failSend();
        // Stopped before it went out: the person's words come back, nothing to report.
        if (signal.aborted) return false;
        if (activeBotId.current === botTarget) {
          setSendError(
            isHostStarting(error)
              ? t`Your machine is starting. Try again in a moment.`
              : userFacingError(error, t`Failed to send message`),
          );
        }
        // Nothing was sent: the composer puts the person's words back.
        return false;
      } finally {
        setUploadStatus(null);
        if (inFlight.current === flight) inFlight.current = null;
        setSending(false);
      }
    },
    [
      beginSend,
      botsRef,
      clearReply,
      hostFeed,
      failSend,
      flushPendingBrowserNotifications,
      pendingAttachments,
      pendingFolders,
      refresh,
      replyQuote,
      sending,
      t,
      userId,
    ],
  );
  const sendCardReply = useCallback(
    (text: string, answer?: CardAnswer) => void sendMessage(text, answer),
    [sendMessage],
  );
  /** Stops the turn, even while a send is on its way: what has not gone out is cancelled (the
   * person's words come back), and a message already posted lands first so its turn is the one
   * stopped. The engine cancels the messages waiting behind the turn and stops the running
   * Helpers. */
  const stopRun = useCallback(async () => {
    const botId = activeBotId.current;
    if (!botId || stopping) return;
    setStopping(true);
    setSendError(null);
    setStopFailed(false);
    const flight = inFlight.current;
    flight?.controller.abort();
    let delivered: boolean;
    let cancelledSend = false;
    try {
      if (flight) {
        const landed = flight.posting
          ? await flight.posting.then(
              () => true,
              () => false,
            )
          : false;
        cancelledSend = !landed;
      }
      ({ delivered } = await rpc.chats.interrupt({ botId }));
    } catch (error) {
      if (activeBotId.current === botId) {
        setSendError(userFacingError(error, t`Failed to stop`));
      }
      return;
    } finally {
      setStopping(false);
    }
    if (activeBotId.current !== botId) return;
    // A send cancelled before it went out started no turn: that stop held with nothing to stop.
    if (delivered || cancelledSend) stopWaiting();
    else setStopFailed(true);
    refresh();
  }, [refresh, stopping, stopWaiting, t]);
  const clearStopFailed = useCallback(() => setStopFailed(false), []);

  function dismissComposerError() {
    // The strip shows one message at a time: a failed send first, then the failed reply.
    setSendError(null);
    if (failure) setDismissedFailureIds((current) => new Set(current).add(failure.id));
  }

  useEffect(() => {
    const threadKey = active?.id;
    // Leaving a chat drops its pending files; their reads stop with them.
    for (const attachment of pendingRef.current) {
      if (attachment.threadKey !== threadKey) abortAttachment(attachment.id);
    }
    setPendingAttachments((current) => {
      const stale = current.filter((attachment) => attachment.threadKey !== threadKey);
      revokePendingAttachmentPreviews(stale);
      return attachmentsForThread(current, threadKey);
    });
    clearReply();
    setAttachmentNotice(null);
    setSendError(null);
  }, [abortAttachment, active?.id, clearReply]);
  return {
    activePendingAttachments,
    activePendingFolders,
    attachFolder,
    removeFolder,
    setReplyTarget,
    setReplyQuote,
    replyTarget,
    replyQuote: replyTarget ? replyQuote : null,
    clearReply,
    sending,
    stopping,
    stopFailed,
    clearStopFailed,
    sendError,
    composerSeed,
    setComposerSeed,
    attachmentNotice,
    uploadStatus,
    fileInputRef,
    followSignal,
    failure,
    dismissComposerError,
    answerMessage,
    onAttachmentPick,
    removeAttachment,
    retryAttachment,
    sendMessage,
    sendCardReply,
    stopRun,
  };
}
