import { Trans, useLingui } from "@lingui/react/macro";
import type { ThreadMessage } from "@superchat/contracts";
import {
  cn,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@superchat/ui-web";
import { Check, Copy, MoreHorizontal, Reply } from "lucide-react";
import { useState } from "react";
import { MessageHoverMetadata } from "../../../components/MessageHoverMetadata";
import { copyableMessageText } from "../../../lib/message-text";

export function MessageHoverActions({
  message,
  side,
  onReply,
  above = false,
}: {
  message: ThreadMessage;
  side: "start" | "end";
  onReply?: (message: ThreadMessage) => void;
  /** Float above the bubble as a toolbar instead of the rail beside it. */
  above?: boolean;
}) {
  const { t } = useLingui();
  const [moreOpen, setMoreOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  // Streaming progress bubbles keep hover free for selection / stop clicks; a reply still being
  // written has nothing to reply to or copy yet.
  if (message.id.startsWith("progress:") || message.id.startsWith("live:")) return null;

  function copyMessage() {
    const text = copyableMessageText(message);
    if (!text || !navigator.clipboard) return;
    void navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => undefined);
  }

  const iconButtonClass =
    "grid h-7 w-7 place-items-center text-muted-foreground transition-colors hover:text-foreground";

  return (
    <MessageHoverMetadata pinned={moreOpen} side={side} above={above}>
      <div data-testid="message-hover-actions" className="flex items-center gap-0.5">
        <button
          type="button"
          aria-label={copied ? t`Copied` : t`Copy`}
          onClick={copyMessage}
          className={cn(
            iconButtonClass,
            "h-11 w-11 [@media(hover:hover)_and_(pointer:fine)]:h-7 [@media(hover:hover)_and_(pointer:fine)]:w-7",
          )}
        >
          {copied ? <Check size={15} strokeWidth={1.7} /> : <Copy size={14} strokeWidth={1.7} />}
        </button>
        {onReply ? (
          <DropdownMenu open={moreOpen} onOpenChange={setMoreOpen}>
            <DropdownMenuTrigger
              aria-label={t`More`}
              className={cn(
                iconButtonClass,
                "h-11 w-11 [@media(hover:hover)_and_(pointer:fine)]:hidden",
              )}
            >
              <MoreHorizontal size={15} strokeWidth={1.7} />
            </DropdownMenuTrigger>
            <DropdownMenuContent align={side === "end" ? "start" : "end"}>
              <DropdownMenuItem onClick={() => onReply(message)}>
                <Reply size={15} />
                <Trans>Reply</Trans>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </div>
    </MessageHoverMetadata>
  );
}
