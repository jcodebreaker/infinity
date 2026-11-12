import { ChatMarkdown } from "@superchat/chat-ui/web";
import type { ThreadMessage } from "@superchat/contracts";
import { personText } from "../../lib/message-text";

/** A plain-text message bubble, matching the Conversation's own bubble tokens
 * (Shell.tsx's `message-user-bubble` / `message-bot-bubble`) since that rendering is
 * defined inline in Shell.tsx and too tied to attachments/reactions/voice to import.
 * Shared by the Side Chat session and the Activity panel's read-only Helper view
 * (ActivityRunDialog.tsx), which need the exact same rendering for their messages. */
export function MessageRow({ message }: { message: ThreadMessage }) {
  const text = message.blocks
    .filter((block): block is { kind: "text"; text: string } => block.kind === "text")
    .map((block) => (message.role === "user" ? personText(block.text) : block.text))
    .join("\n\n");
  if (!text) return null;
  if (message.role === "system") {
    return (
      <p className="self-center text-[12.5px] text-muted-foreground" dir="auto">
        {text}
      </p>
    );
  }
  if (message.role === "user") {
    return (
      <div className="flex w-fit max-w-full justify-end">
        <div
          className="max-w-full rounded-[20px] rounded-ee-[6px] bg-bubble px-4 py-2.5 text-[15.5px] leading-[1.6] whitespace-pre-wrap wrap-anywhere text-chat-user-foreground"
          dir="auto"
        >
          {text}
        </div>
      </div>
    );
  }
  return (
    <div className="flex w-fit max-w-full justify-start">
      <div className="max-w-full text-[15.5px] leading-[1.6] text-foreground" dir="auto">
        <ChatMarkdown>{text}</ChatMarkdown>
      </div>
    </div>
  );
}
