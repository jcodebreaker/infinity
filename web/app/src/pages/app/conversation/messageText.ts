import { t } from "@lingui/core/macro";
import type { ThreadMessage } from "@superchat/contracts";
import { parseAttachmentReferences, plainTextFromMarkdown } from "@superchat/core";
import { splitComposerAttachments } from "../../../lib/composer-attachments";

export function previewMessageText(message: ThreadMessage): string {
  const text = message.blocks
    .map((block) => {
      if (block.kind === "text") {
        // Bot text is Markdown; user text is already plain.
        if (message.role === "bot") return plainTextFromMarkdown(block.text);
        const files = parseAttachmentReferences(block.text);
        const first = files.attachments[0]?.path.split("/").pop() ?? "";
        const ask = splitComposerAttachments(files.caption);
        if (ask.chips.length) return ask.rest || (ask.chips[0]?.label ?? "");
        return files.caption || first;
      }
      return "";
    })
    .filter(Boolean)
    .join(" ")
    .trim();
  if (text) return text;
  if (message.blocks.some((block) => block.kind === "image" || block.kind === "file")) {
    return t`Attachment`;
  }
  return t`Message`;
}

/**
 * Skill offers apply their own effect, so they stay answerable after their run ends;
 * asks.answer checks they're still open.
 */
export function hasOpenMuseAsk(message: ThreadMessage): boolean {
  return message.blocks.some(
    (block) => block.kind === "ask" && block.status !== "answered" && Boolean(block.skillOffer),
  );
}
