import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import type { ThreadMessage } from "@superchat/contracts";
import { CircleAlert, Info } from "lucide-react";
import type { ReactNode } from "react";
import { useId, useState } from "react";
import { requestOpenSettings } from "../../../lib/open-settings";

/** The engine sends a failure's code, never its wording: the copy is ours. */
export function useFailureNoteText(): (code: string) => string {
  const { t } = useLingui();
  return (code) => {
    switch (code) {
      case "provider_unavailable":
        return t`I lost the connection. Try again.`;
      case "timeout":
        return t`That took too long. Try again.`;
      case "rate_limited":
      case "overloaded":
        return t`The model is busy. Try again in a moment.`;
      case "insufficient_credit":
        return t`The model account is out of credit.`;
      case "auth_failed":
        return t`The model refused my key.`;
      case "context_too_long":
        return t`This conversation is too long for the model.`;
      case "sandbox_unavailable":
        return t`My machine restarted. Try again.`;
      case "model_key_required":
        return t`I need your API key to think.`;
      case "model_budget_exhausted":
        return t`You've reached your monthly model budget.`;
      case "account_suspended":
        return t`Your account is paused. Contact your admin.`;
      case "model_not_supported":
        return t`This model isn't available — pick another.`;
      case "model_provider_unreachable":
        return t`I couldn't reach the model provider. Try again.`;
      case "reply_not_saved":
        return t`I couldn't save that reply, so it won't be here when you come back.`;
      default:
        return t`Something went wrong on my side. Try again.`;
    }
  };
}

/** The one thing a failure note can offer to fix itself: open Settings where it is fixed. */
export function useFailureNoteAction(): (
  code: string,
) => { label: string; onClick: () => void } | null {
  const { t } = useLingui();
  return (code) => {
    switch (code) {
      case "model_key_required":
        return { label: t`Add your API key`, onClick: () => requestOpenSettings("models") };
      case "model_budget_exhausted":
        return { label: t`Raise budget`, onClick: () => requestOpenSettings("models") };
      default:
        return null;
    }
  };
}

/** An info notice's copy (the engine's `error` item at `level: info`): something that happened,
 * not a failed reply, so it never says "Try again". `null` for a code with nothing to tell. */
export function useInfoNoteText(): (code: string) => string | null {
  const { t } = useLingui();
  return (code) => {
    switch (code) {
      case "workspace_reset":
        return t`My machine was replaced, so files I hadn't saved elsewhere are gone.`;
      default:
        return null;
    }
  };
}

const ROW = "flex min-h-7 items-center gap-1.5 text-[12.5px] leading-[1.4] text-ink-3";

function FailureIcon() {
  return <CircleAlert size={13} strokeWidth={1.75} aria-hidden="true" className="shrink-0" />;
}

/** An info notice as the same quiet row, with an info mark instead of the alert. */
export function InfoNote({ children }: { children: ReactNode }) {
  return (
    <div className={ROW}>
      <Info size={13} strokeWidth={1.75} aria-hidden="true" className="shrink-0" />
      <p data-testid="message-info-note" className="min-w-0">
        {children}
      </p>
    </div>
  );
}

/** One failed reply as a quiet inline row on Superchat's side: a small mark and the note. */
export function FailureNote({
  children,
  action,
}: {
  children: ReactNode;
  /** A fix the note can open (a key, a budget), as a quiet link after the words. */
  action?: { label: string; onClick: () => void } | null;
}) {
  return (
    <div className={ROW}>
      <FailureIcon />
      <p data-testid="message-error-note" className="min-w-0">
        {children}
      </p>
      {action ? (
        <button
          type="button"
          data-testid="message-error-action"
          onClick={action.onClick}
          className="shrink-0 rounded-md px-1 py-0.5 text-link transition-colors hover:underline focus-visible:outline-2 focus-visible:outline-ring"
        >
          {action.label}
        </button>
      ) : null}
    </div>
  );
}

function clockTime(iso: string, locale: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString(locale || "en", { hour: "numeric", minute: "2-digit" });
}

/**
 * Several failed replies in a row (transcript `failures` rows, failureNotes.ts) as one line:
 * "3 failed attempts", and a small toggle that lists each note with its time.
 */
export function FailureRun({ messages }: { messages: readonly ThreadMessage[] }) {
  const { t, i18n } = useLingui();
  const noteText = useFailureNoteText();
  const [open, setOpen] = useState(false);
  const listId = useId();
  return (
    <div data-testid="failure-run" className="flex flex-col">
      <div className={ROW}>
        <FailureIcon />
        <span className="min-w-0 truncate">
          {plural(messages.length, { one: "# failed attempt", other: "# failed attempts" })}
        </span>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((value) => !value)}
          className="rounded-md px-1 py-0.5 text-[12.5px] text-link transition-colors hover:underline focus-visible:outline-2 focus-visible:outline-ring"
        >
          {open ? t`Hide` : t`Show`}
        </button>
      </div>
      {open ? (
        <ul
          id={listId}
          className="ms-[7px] mt-1 flex flex-col gap-1 border-s border-line ps-[15px]"
        >
          {messages.map((message) => (
            <li
              key={message.id}
              className="flex items-baseline justify-between gap-4 text-[13px] text-ink-2"
            >
              <span data-testid="message-error-note" className="min-w-0">
                {message.blocks.map((block) =>
                  block.kind === "error" ? noteText(block.code) : null,
                )}
              </span>
              <time
                dateTime={message.createdAt}
                className="shrink-0 font-mono text-[11.5px] tabular-nums text-ink-3"
              >
                {clockTime(message.createdAt, i18n.locale)}
              </time>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
