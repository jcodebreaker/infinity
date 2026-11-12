import { i18n } from "@lingui/core";
import { useLingui } from "@lingui/react/macro";
import type { ThreadMessage } from "@superchat/contracts";
import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { copyableMessageText } from "../../../lib/message-text";

/** Under the person's own message: when it was sent, and a copy button. */
export function UserMessageMeta({ message }: { message: ThreadMessage }) {
  const { t } = useLingui();
  const [copied, setCopied] = useState(false);
  const sent = new Date(message.createdAt);
  const text = copyableMessageText(message);
  return (
    <div
      data-testid="user-message-meta"
      className="mt-1.5 flex items-center justify-end gap-1 text-[12px] text-ink-3"
    >
      <time dateTime={message.createdAt} className="tabular-nums whitespace-nowrap">
        {sent.toLocaleString(i18n.locale || "en", {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        })}
      </time>
      {text && navigator.clipboard ? (
        <button
          type="button"
          aria-label={copied ? t`Copied` : t`Copy message`}
          title={copied ? t`Copied` : t`Copy message`}
          onClick={() => {
            void navigator.clipboard
              .writeText(text)
              .then(() => {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              })
              .catch(() => undefined);
          }}
          className="grid size-7 place-items-center rounded-lg transition-colors hover:bg-foreground/10 hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
        >
          {copied ? (
            <Check size={14} strokeWidth={2} aria-hidden="true" />
          ) : (
            <Copy size={14} strokeWidth={1.8} aria-hidden="true" />
          )}
        </button>
      ) : null}
    </div>
  );
}
