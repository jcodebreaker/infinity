import { useLingui } from "@lingui/react/macro";
import { Button, cn } from "@superchat/ui-web";
import { ArrowUp, Loader2, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { AskStatus } from "./useArtifactAsk";

/** One selected part of the artifact, shown as a chip inside the box. */
export type AskChip = { id: string; label: string; detail?: string };

/**
 * The message box for asking superchat to change the artifact that is open: the selection as
 * chips, the person's words, send (or stop while superchat works), and one quiet status line
 * above it. Kind-agnostic: the kind supplies the chips and the request block (`useArtifactAsk`).
 */
export function AskBox({
  chips,
  scope,
  onRemoveChip,
  placeholder,
  focusRequest,
  status,
  onSend,
  onStop,
  onUndo,
  canUndo,
  className,
  note,
}: {
  chips: readonly AskChip[];
  /** With no chip, what the request is about ("Slide 2"): a quiet chip that cannot be removed. */
  scope?: string;
  onRemoveChip?: (id: string) => void;
  placeholder: string;
  /** Bumped to focus the box (the toolbar's Ask, the selection chip's Ask). */
  focusRequest: number;
  status: AskStatus;
  onSend: (text: string) => Promise<boolean>;
  onStop: () => void;
  onUndo?: () => void;
  canUndo?: boolean;
  className?: string;
  /** Why the request waits ("Finishing another file first"): the status line while it does. */
  note?: string | null;
}) {
  const { t } = useLingui();
  const [draft, setDraft] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);
  const handled = useRef(focusRequest);
  useEffect(() => {
    if (focusRequest === handled.current) return;
    handled.current = focusRequest;
    textarea.current?.focus();
  }, [focusRequest]);

  const working = status.kind === "sending" || status.kind === "editing";
  const send = async () => {
    const text = draft.trim();
    if (!text || working) return;
    setDraft("");
    if (!(await onSend(text))) setDraft(text);
  };

  return (
    <div className={cn("flex w-full flex-col gap-2", className)} data-testid="ask-box">
      <AskStatusRow
        status={status}
        note={note}
        onStop={onStop}
        onUndo={canUndo ? onUndo : undefined}
      />
      <div className="sc-ask" data-state={working ? "editing" : status.kind}>
        <div className="sc-ask-body flex flex-col gap-2.5 py-3 ps-4 pe-3">
          {chips.length ? (
            <div className="flex flex-wrap gap-1.5">
              {chips.map((chip) => (
                <span
                  key={chip.id}
                  data-testid="ask-chip"
                  title={chip.detail}
                  className="inline-flex h-6 max-w-full items-center gap-1 rounded-[7px] bg-selection-strong ps-2 pe-1 text-[12px] text-tint-ink"
                >
                  <span className="truncate">{chip.label}</span>
                  {onRemoveChip ? (
                    <button
                      type="button"
                      aria-label={t`Remove ${chip.label}`}
                      onClick={() => onRemoveChip(chip.id)}
                      className="grid size-4 place-items-center rounded opacity-70 hover:opacity-100"
                    >
                      <X size={11} strokeWidth={2.2} />
                    </button>
                  ) : null}
                </span>
              ))}
            </div>
          ) : scope ? (
            <div className="flex">
              <span
                data-testid="ask-scope"
                className="inline-flex h-6 max-w-full items-center rounded-[7px] bg-selection-strong px-2 text-[12px] text-tint-ink opacity-70"
              >
                <span className="truncate">{scope}</span>
              </span>
            </div>
          ) : null}
          <div className="flex items-end gap-3">
            <textarea
              ref={textarea}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                const composing = event.nativeEvent.isComposing || event.keyCode === 229;
                if (event.key === "Enter" && !event.shiftKey && !composing) {
                  event.preventDefault();
                  void send();
                }
              }}
              rows={1}
              dir="auto"
              aria-label={t`Ask superchat`}
              placeholder={placeholder}
              className="max-h-32 min-h-[24px] flex-1 resize-none bg-transparent py-1 text-[15px] leading-[1.45] text-foreground outline-none [field-sizing:content] placeholder:text-muted-foreground"
            />
            {working ? (
              <Button
                size="icon"
                aria-label={t`Stop`}
                onClick={onStop}
                className="size-8 shrink-0 rounded-full"
              >
                <Square size={10} strokeWidth={0} fill="currentColor" />
              </Button>
            ) : (
              <Button
                size="icon"
                aria-label={t`Send`}
                disabled={!draft.trim()}
                onClick={() => void send()}
                className="size-8 shrink-0 rounded-full transition-transform active:scale-95"
              >
                <ArrowUp size={16} strokeWidth={2.2} />
              </Button>
            )}
          </div>
        </div>
      </div>
      <p className="truncate ps-1 text-[11px] text-muted-foreground" dir="auto">
        {working
          ? t`You asked: ${status.asked}`
          : chips.length
            ? t`Enter to send · Shift+Enter for a new line · superchat changes only what you selected`
            : t`Enter to send · Shift+Enter for a new line`}
      </p>
    </div>
  );
}

function AskStatusRow({
  status,
  note,
  onStop,
  onUndo,
}: {
  status: AskStatus;
  note?: string | null;
  onStop: () => void;
  onUndo?: () => void;
}) {
  const { t } = useLingui();
  const row = "flex h-[22px] items-center gap-2 ps-1 text-[12px] text-muted-foreground";
  const working = status.kind === "sending" || status.kind === "editing";
  if (working || note) {
    return (
      <div role="status" data-testid="ask-status" className={row}>
        <Loader2 size={12} className="animate-spin text-tint motion-reduce:animate-none" />
        <span className="truncate">{note || t`superchat is editing…`}</span>
        {working ? (
          <button
            type="button"
            onClick={onStop}
            className="ms-auto rounded-md border border-border px-2 py-0.5 text-foreground hover:bg-accent"
          >
            {t`Stop`}
          </button>
        ) : null}
      </div>
    );
  }
  if (status.kind === "idle") return <div aria-hidden="true" className="h-[22px]" />;
  if (status.kind === "updated") {
    return (
      <div role="status" data-testid="ask-status" className={row}>
        <span aria-hidden="true" className="size-1.5 rounded-full bg-success" />
        <span className="truncate">{t`Updated · version ${status.version}`}</span>
        {onUndo ? (
          <button
            type="button"
            onClick={onUndo}
            className="text-link underline-offset-2 hover:underline"
          >
            {t`Undo`}
          </button>
        ) : null}
      </div>
    );
  }
  if (status.kind === "replied") {
    return (
      <div role="status" data-testid="ask-status" className={row} title={status.reply}>
        <span className="truncate" dir="auto">
          {status.reply || t`No change was made.`}
        </span>
      </div>
    );
  }
  return (
    <div role="status" data-testid="ask-status" className={cn(row, "text-destructive")}>
      <span className="truncate">{status.message || t`Couldn't send that.`}</span>
    </div>
  );
}
