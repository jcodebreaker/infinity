import { useLingui } from "@lingui/react/macro";
import type { ThreadMessage } from "@superchat/contracts";
import type { SlashActionId } from "@superchat/core";
import { SLASH_ACTIONS } from "@superchat/core";
import {
  Button,
  cn,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@superchat/ui-web";
import {
  AlertCircle,
  ArrowUp,
  Folder,
  Loader2,
  Paperclip,
  Plus,
  Settings,
  Square,
  Upload,
  X,
} from "lucide-react";
import {
  type ClipboardEvent,
  type DragEvent,
  memo,
  type RefObject,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ComposerAttachmentChip } from "../../../components/ComposerAttachmentChip";
import type { WorkPhase } from "../../../features/activity";
import {
  clearComposerAttachments,
  setComposerAttachment,
  useComposerAttachments,
  withComposerAttachments,
} from "../../../lib/composer-attachments";
import { isFileDrag, isFilePaste } from "../../../lib/pending-attachments";
import { useFailureNoteAction, useFailureNoteText } from "./FailureNote";
import { previewMessageText } from "./messageText";
import {
  ATTACHMENT_ACCEPT,
  attachmentsBlockSend,
  NO_ATTACHMENTS,
  type PendingAttachment,
} from "./shared";
import { slashActionLabel } from "./slashActionLabel";

const NO_FOLDERS: readonly string[] = [];

export const Composer = memo(function Composer({
  activeName,
  phase,
  stopFailed = false,
  pendingAttachments = NO_ATTACHMENTS,
  pendingFolders = NO_FOLDERS,
  onAttachFolder,
  onRemoveFolder,
  attachmentNotice,
  uploadStatus,
  sendError,
  failure,
  onDismissError,
  fileInputRef,
  onAttachmentPick,
  onRemoveAttachment,
  onRetryAttachment,
  onSend,
  onStop,
  placeholder,
  replyTarget,
  replyQuote,
  replyTargetName,
  onClearReply,
  onSlashAction,
  seedText,
  onSeedConsumed,
}: {
  activeName?: string;
  /** The Conversation's work state (features/activity `useWorkState`): Stop shows while it is not
   * idle and is held while a Stop is on its way; Send waits while a message or a Stop is. */
  phase: WorkPhase;
  /** The last Stop did not land, while the work still runs. */
  stopFailed?: boolean;
  pendingAttachments?: PendingAttachment[];
  /** Workspace folders the next message points at. */
  pendingFolders?: readonly string[];
  /** Opens the folder picker; the + then offers "Attach a folder" beside uploads. */
  onAttachFolder?: () => void;
  onRemoveFolder?: (path: string) => void;
  attachmentNotice?: string | null;
  /** Progress while attachments are written to the host ("Uploading 2 of 3"). */
  uploadStatus?: string | null;
  sendError?: string | null;
  /** The latest reply failed for a model-layer reason the person can fix (a key, a budget). */
  failure?: { id: string; code: string } | null;
  onDismissError?: () => void;
  /** Without an attachment handler there is no attach control. */
  fileInputRef?: RefObject<HTMLInputElement | null>;
  onAttachmentPick?: (files: FileList | null) => void | Promise<void>;
  onRemoveAttachment?: (attachment: PendingAttachment) => void;
  onRetryAttachment?: (attachment: PendingAttachment) => void;
  /** Resolves `false` when nothing was sent, so the composer gives the person's words back. */
  onSend: (text: string) => Promise<boolean | undefined | void>;
  onStop?: () => Promise<void>;
  /** Overrides the "Message {name}" placeholder. */
  placeholder?: string;
  replyTarget?: ThreadMessage | null;
  replyQuote?: string | null;
  replyTargetName?: string;
  onClearReply?: () => void;
  onSlashAction?: (action: SlashActionId) => void;
  /** First-run "Try it" chips (pages/app/intro): fills the draft and focuses it, fill-then-send. */
  seedText?: string | null;
  onSeedConsumed?: () => void;
}) {
  const { t } = useLingui();
  const [draft, setDraft] = useState("");
  const attachments = useComposerAttachments();
  const [slashQuery, setSlashQuery] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [replyAnnouncement, setReplyAnnouncement] = useState("");
  // What the live region currently holds — a send disarming the reply clears
  // "reply" text, while an explicit cancel must keep "Reply cancelled".
  const replyAnnouncementKind = useRef<"reply" | "cancelled" | null>(null);
  const prevReplyTarget = useRef<ThreadMessage | null>(null);
  const dragDepth = useRef(0);
  const [draggingFiles, setDraggingFiles] = useState(false);
  // A failed turn the person can fix themselves: the note offers where (Settings > Models).
  const failureText = useFailureNoteText();
  const failureAction = useFailureNoteAction();
  const failureNote = failure ? failureText(failure.code) : null;
  const fix = failure ? failureAction(failure.code) : null;
  // A message or a Stop on its way holds the next send.
  const busy = phase === "sending" || phase === "stopping";
  const canSend =
    draft.trim().length > 0 || pendingAttachments.length > 0 || pendingFolders.length > 0;
  // A file still being read, or one that failed, holds Send until it is ready, removed or retried.
  const attachmentsPending = attachmentsBlockSend(pendingAttachments);

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;

    function syncHeight() {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.style.height = "0px";
      textarea.style.height = `${textarea.scrollHeight}px`;
    }

    syncHeight();
    let lastWidth = el.getBoundingClientRect().width;
    const observer = new ResizeObserver(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      const width = textarea.getBoundingClientRect().width;
      if (width === lastWidth) return;
      lastWidth = width;
      syncHeight();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [draft]);

  function updateDraft(value: string) {
    setDraft(value);
    // `/` only at the start of the draft.
    const slashMatch = /^\/([^\n]*)$/.exec(value);
    setSlashQuery(slashMatch ? (slashMatch[1] ?? "") : null);
  }

  function focusComposer() {
    textareaRef.current?.focus();
  }

  // First-run "Try it" chips seed the draft rather than sending it (fill-then-focus, so
  // the person sees it first); the parent clears `seedText` once consumed so it can't
  // re-fire on an unrelated re-render.
  useEffect(() => {
    if (!seedText) return;
    updateDraft(seedText);
    focusComposer();
    onSeedConsumed?.();
  }, [seedText]);

  function runSlashAction(action: SlashActionId) {
    setDraft("");
    setSlashQuery(null);
    onSlashAction?.(action);
  }

  const slashActionOptions = useMemo(() => {
    if (slashQuery === null) return [];
    const query = slashQuery.trim().toLowerCase();
    return SLASH_ACTIONS.filter((action) => !query || action.label.toLowerCase().includes(query));
  }, [slashQuery]);

  const showSlashPicker = slashQuery !== null && slashActionOptions.length > 0;

  function send() {
    if (!canSend || busy || attachmentsPending) return;
    const text = draft.replace(/^\s+/, "").trimEnd();
    const kept = { draft, attachments };
    setDraft("");
    setSlashQuery(null);
    clearComposerAttachments();
    void onSend(withComposerAttachments(text, attachments)).then((sent) => {
      if (sent !== false) return;
      // The send failed (the host never started, say): nothing is lost.
      setDraft((current) => (current ? current : kept.draft));
      for (const item of kept.attachments) setComposerAttachment(item.kind, item);
    });
  }

  function handleDragEnter(event: DragEvent<HTMLFieldSetElement>) {
    const dataTransfer = event.dataTransfer;
    if (!isFileDrag(dataTransfer)) return;
    event.preventDefault();
    dragDepth.current += 1;
    setDraggingFiles(true);
  }

  function handleDragOver(event: DragEvent<HTMLFieldSetElement>) {
    const dataTransfer = event.dataTransfer;
    if (!isFileDrag(dataTransfer)) return;
    event.preventDefault();
    dataTransfer.dropEffect = "copy";
    setDraggingFiles(true);
  }

  function handleDragLeave(event: DragEvent<HTMLFieldSetElement>) {
    if (!isFileDrag(event.dataTransfer)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDraggingFiles(false);
  }

  function handleDrop(event: DragEvent<HTMLFieldSetElement>) {
    const dataTransfer = event.dataTransfer;
    if (!isFileDrag(dataTransfer)) return;
    event.preventDefault();
    dragDepth.current = 0;
    setDraggingFiles(false);
    void onAttachmentPick?.(dataTransfer.files);
  }

  function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const clipboardData = event.clipboardData;
    // Only intercept real FileList pastes; leave text-only / empty-files native.
    if (!onAttachmentPick || !clipboardData || !isFilePaste(clipboardData)) return;
    event.preventDefault();
    void onAttachmentPick(clipboardData.files);
    const text = clipboardData.getData("text/plain");
    if (!text) return;
    const textarea = event.currentTarget;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    updateDraft(`${draft.slice(0, start)}${text}${draft.slice(end)}`);
    const caret = start + text.length;
    window.requestAnimationFrame(() => {
      textareaRef.current?.setSelectionRange(caret, caret);
    });
  }

  const showComposerPlaceholder = draft.length === 0;
  const replyName = replyTarget ? (replyTargetName ?? previewMessageText(replyTarget)) : "";
  const replyNameRef = useRef(replyName);
  replyNameRef.current = replyName;
  const announceTimer = useRef(0);
  useEffect(() => () => window.clearTimeout(announceTimer.current), []);

  // Arming or retargeting a reply unmounts the control that started it, so
  // focus would drop to <body>; announce on start and when the target changes.
  // The timer lives in a ref: a same-id rerender (e.g. the display name
  // resolving after arming) must not cancel the pending announcement.
  useEffect(() => {
    const prev = prevReplyTarget.current;
    prevReplyTarget.current = replyTarget ?? null;
    if (!replyTarget) {
      // Cancel (or send) within the delay must not let a stale "Replying to"
      // overwrite the cancel announcement — kill the pending timer.
      window.clearTimeout(announceTimer.current);
      // A send disarms the reply without touching the region — drop the stale
      // "Replying to" so it cannot linger or re-announce. An explicit cancel
      // sets "Reply cancelled" in the same event, so only clear "reply" text.
      if (replyAnnouncementKind.current === "reply") {
        replyAnnouncementKind.current = null;
        setReplyAnnouncement("");
      }
      return;
    }
    if (!prev || prev.id !== replyTarget.id) {
      textareaRef.current?.focus();
      // Clear-then-set so a switch between same-author targets re-announces —
      // identical live-region text would otherwise be a no-op.
      setReplyAnnouncement("");
      replyAnnouncementKind.current = null;
      window.clearTimeout(announceTimer.current);
      announceTimer.current = window.setTimeout(() => {
        replyAnnouncementKind.current = "reply";
        setReplyAnnouncement(t`Replying to ${replyNameRef.current}`);
      }, 50);
    }
  }, [replyTarget, replyName, t]);

  return (
    <fieldset
      aria-label={t`Message composer`}
      data-dragging={draggingFiles ? "files" : undefined}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      className={cn(
        "relative z-30 m-0 min-w-0 border-0 px-3 pb-4 pt-3 md:px-6 md:pb-6",
        "mx-auto w-full max-w-[748px] pt-2 md:pb-[18px]",
        draggingFiles && "rounded-[14px] ring-2 ring-inset ring-ring",
      )}
    >
      <div role="status" data-testid="composer-announcement" className="sr-only">
        {replyAnnouncement}
      </div>
      {sendError || failureNote ? (
        <div
          role="alert"
          data-testid="composer-error"
          className="mb-3 flex items-center gap-2 rounded-[14px] border border-destructive/40 bg-destructive/10 px-4 py-2 text-[13px] text-destructive"
        >
          <span className="min-w-0 flex-1">{sendError ?? failureNote}</span>
          {!sendError && fix ? (
            <button
              type="button"
              data-testid="composer-error-action"
              onClick={fix.onClick}
              className="shrink-0 rounded-md px-1 py-0.5 font-medium underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
            >
              {fix.label}
            </button>
          ) : null}
          <button
            type="button"
            aria-label={t`Dismiss error`}
            data-testid="composer-error-dismiss"
            onClick={() => {
              onDismissError?.();
              window.requestAnimationFrame(() => textareaRef.current?.focus());
            }}
            className="shrink-0 text-destructive hover:text-foreground"
          >
            <X size={13} strokeWidth={2} />
          </button>
        </div>
      ) : null}
      {replyTarget ? (
        <div
          data-testid="reply-chip"
          className="mb-2 flex items-center gap-2 rounded-full border border-border bg-muted px-3 py-1.5 text-[13px] text-foreground/75"
        >
          <span className="min-w-0 flex-1 truncate text-muted-foreground">
            {replyQuote
              ? t`Replying to ${replyName}: “${replyQuote}”`
              : t`Replying to ${replyName}`}
          </span>
          <button
            type="button"
            aria-label={t`Cancel reply`}
            onClick={() => {
              replyAnnouncementKind.current = "cancelled";
              // Kill a pending arm announce in this event — the effect's
              // cleanup can lag the timer, and a late "Replying to" would
              // then be cleared as stale, dropping the cancel announcement.
              window.clearTimeout(announceTimer.current);
              onClearReply?.();
              setReplyAnnouncement(t`Reply cancelled`);
              // The chip unmounts with this button — keep focus in the composer.
              textareaRef.current?.focus();
            }}
            className="shrink-0 text-muted-foreground hover:text-foreground"
          >
            <X size={13} strokeWidth={2} />
          </button>
        </div>
      ) : null}
      {attachments.map((attachment) => (
        <ComposerAttachmentChip
          key={attachment.kind}
          kind={attachment.kind}
          label={attachment.label}
          className="mb-2 w-full"
        >
          <button
            type="button"
            aria-label={t`Remove selection`}
            onClick={() => setComposerAttachment(attachment.kind, null)}
            className="shrink-0 text-muted-foreground hover:text-foreground"
          >
            <X size={13} strokeWidth={2} />
          </button>
        </ComposerAttachmentChip>
      ))}
      {attachmentNotice ? (
        <div className="mb-3 rounded-[14px] border border-warning/40 bg-warning/10 px-4 py-2 text-[13px] text-warning">
          {attachmentNotice}
        </div>
      ) : null}
      {uploadStatus || stopFailed ? (
        <div
          role="status"
          data-testid="composer-upload-status"
          className="mb-3 px-1 text-[13px] text-muted-foreground"
        >
          {stopFailed ? t`Couldn't stop` : uploadStatus}
        </div>
      ) : null}
      {pendingFolders.length ? (
        <div className="mb-3 flex flex-wrap gap-2">
          {pendingFolders.map((folder) => (
            <div
              key={folder}
              data-testid="composer-folder"
              className="flex items-center gap-2 rounded-full border border-tint-ink/40 bg-tint-ink/10 px-3 py-1.5 text-[13px] text-foreground"
            >
              <Folder size={14} strokeWidth={1.8} aria-hidden="true" className="text-tint-ink" />
              <span className="max-w-[240px] truncate" dir="auto">
                {folder ? folder.split("/").pop() : t`Whole workspace`}
              </span>
              {onRemoveFolder ? (
                <button
                  type="button"
                  aria-label={t`Remove folder ${folder || t`Whole workspace`}`}
                  onClick={() => onRemoveFolder(folder)}
                  className="text-muted-foreground hover:text-foreground"
                >
                  <X size={12} strokeWidth={2} />
                </button>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {pendingAttachments.length ? (
        <div className="mb-3 flex flex-wrap gap-2">
          {pendingAttachments.map((attachment) => (
            <div
              key={attachment.id}
              data-testid="composer-attachment"
              data-status={attachment.status ?? "ready"}
              className={cn(
                "flex items-center gap-2 rounded-full border border-border bg-muted px-3 py-1.5 text-[13px] text-foreground/75",
                attachment.status === "error" && "border-warning/40",
              )}
            >
              {attachment.previewUrl ? (
                <img
                  src={attachment.previewUrl}
                  alt={attachment.file.name}
                  className="h-8 w-8 rounded object-cover"
                />
              ) : (
                <Paperclip size={14} strokeWidth={1.8} />
              )}
              <span className="max-w-[180px] truncate" dir="auto">
                {attachment.status === "uploading"
                  ? t`Uploading ${attachment.file.name}…`
                  : attachment.status === "starting"
                    ? t`Starting your machine…`
                    : attachment.status === "reading"
                      ? t`Reading ${attachment.file.name}…`
                      : attachment.file.name}
              </span>
              {attachment.status === "uploading" ||
              attachment.status === "starting" ||
              attachment.status === "reading" ? (
                <Loader2
                  size={13}
                  strokeWidth={2}
                  aria-hidden="true"
                  className="animate-spin text-muted-foreground motion-reduce:animate-none"
                />
              ) : null}
              {attachment.status === "error" ? (
                <>
                  <span
                    role="alert"
                    className="flex max-w-[220px] items-center gap-1 truncate text-[12px] text-warning"
                  >
                    <AlertCircle size={13} strokeWidth={2} aria-hidden="true" />
                    {attachment.error}
                  </span>
                  <button
                    type="button"
                    onClick={() => onRetryAttachment?.(attachment)}
                    className="text-[12px] font-medium text-foreground underline-offset-2 hover:underline"
                  >
                    {t`Retry`}
                  </button>
                </>
              ) : null}
              <button
                type="button"
                aria-label={t`Remove ${attachment.file.name}`}
                onClick={() => onRemoveAttachment?.(attachment)}
                className="text-muted-foreground hover:text-foreground"
              >
                <X size={13} strokeWidth={2} />
              </button>
            </div>
          ))}
        </div>
      ) : null}
      {showSlashPicker ? (
        <div
          data-testid="slash-picker"
          className="mb-2 overflow-hidden rounded-[14px] border border-border bg-muted"
        >
          {slashActionOptions.map((action) => {
            const label = slashActionLabel(action.id);
            return (
              <button
                key={action.id}
                type="button"
                aria-label={label}
                onClick={() => runSlashAction(action.id)}
                className="flex w-full items-center gap-3 px-4 py-2.5 text-start hover:bg-accent"
              >
                <Settings size={16} strokeWidth={1.7} className="shrink-0 text-muted-foreground" />
                <span className="text-[14px] text-foreground">{label}</span>
              </button>
            );
          })}
        </div>
      ) : null}
      <div
        data-testid="composer-bar"
        className={cn(
          "flex gap-3.5 border border-border bg-background py-[9px] pe-2.5 ps-3 transition-[border-color,box-shadow] duration-150 focus-within:border-ring",
          "sc-composer min-h-[108px] flex-wrap content-between items-end gap-x-1.5 gap-y-2 rounded-[20px] px-3 pt-3.5 pb-2.5",
        )}
      >
        {onAttachmentPick ? (
          <>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ATTACHMENT_ACCEPT}
              className="hidden"
              onChange={(event) => void onAttachmentPick(event.target.files)}
            />
            {onAttachFolder ? (
              <DropdownMenu>
                <DropdownMenuTrigger
                  aria-label={t`Attach`}
                  className="grid size-[34px] shrink-0 place-items-center rounded-full text-ink-3 transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
                >
                  <Plus size={16} strokeWidth={2} />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" side="top" className="min-w-60">
                  <DropdownMenuItem onClick={() => fileInputRef?.current?.click()}>
                    <Upload size={15} aria-hidden="true" />
                    {t`Upload from this device`}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={onAttachFolder}>
                    <Folder size={15} aria-hidden="true" />
                    {t`Attach a folder from your machine`}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : (
              <Button
                variant="ghost"
                size="icon"
                aria-label={t`Attach file`}
                onClick={() => fileInputRef?.current?.click()}
                className={cn(
                  "size-8 shrink-0 rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                  "size-[34px] border-0 bg-transparent text-ink-3",
                )}
              >
                <Plus size={16} strokeWidth={2} />
              </Button>
            )}
          </>
        ) : null}
        <div
          className={cn(
            "flex min-w-0 flex-1 flex-wrap items-end gap-1.5",
            "order-first basis-full px-1.5",
          )}
        >
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => updateDraft(event.target.value)}
            onPaste={handlePaste}
            onKeyDown={(event) => {
              const composing = event.nativeEvent.isComposing || event.keyCode === 229;
              if (event.key === "Enter" && !event.shiftKey && !composing) {
                event.preventDefault();
                send();
              }
            }}
            placeholder={
              showComposerPlaceholder
                ? (placeholder ?? (activeName ? t`What can I help you with?` : t`Message…`))
                : undefined
            }
            aria-label={t`Message superchat`}
            name="chat-message"
            autoComplete="off"
            dir="auto"
            rows={1}
            className={cn(
              "min-h-[24px] min-w-[8rem] flex-1 resize-none overflow-y-auto bg-transparent py-0.5 leading-6 text-foreground outline-none placeholder:text-muted-foreground disabled:opacity-40",
              "max-h-[200px] min-h-[44px] text-[15px] placeholder:text-ink-3",
            )}
          />
        </div>
        {phase !== "idle" ? (
          <div className={"ms-auto flex shrink-0 items-center gap-1.5"}>
            <Button
              size="icon"
              aria-label={t`Send`}
              disabled={busy || !canSend || attachmentsPending}
              onClick={send}
              className={cn(
                "size-8 rounded-full shadow-sm transition-transform active:scale-95",
                "size-[34px] bg-tint text-white shadow-none hover:bg-tint hover:brightness-110",
              )}
            >
              <ArrowUp size={16} strokeWidth={2.2} />
            </Button>
            <Button
              variant="outline"
              size="icon"
              aria-label={t`Stop`}
              disabled={phase === "stopping"}
              onClick={() => void onStop?.()}
              className="size-8 rounded-full border border-border bg-muted text-foreground/80 shadow-sm transition-colors hover:bg-accent hover:text-foreground"
            >
              <Square size={11} strokeWidth={0} fill="currentColor" />
            </Button>
          </div>
        ) : (
          <Button
            size="icon"
            aria-label={t`Send`}
            disabled={busy || !canSend || attachmentsPending}
            onClick={send}
            className={cn(
              "size-8 shrink-0 rounded-full shadow-sm transition-transform active:scale-95",
              "ms-auto size-[34px] bg-tint text-white shadow-none hover:bg-tint hover:brightness-110 disabled:bg-selection disabled:text-ink-3 disabled:opacity-100",
            )}
          >
            <ArrowUp size={16} strokeWidth={2.2} />
          </Button>
        )}
      </div>
    </fieldset>
  );
});
