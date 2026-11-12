import type { ReplyCardBlock, ThreadMessage } from "@superchat/contracts";

/** What one card block in the transcript should draw. */
export type ResolvedReplyCard = {
  /** `null` hides it: a later card with the same `id` updates the first one in place. */
  block: ReplyCardBlock | null;
  /** The person's reply to the message holding this card (an `ask` card locks on it). */
  answer?: string;
};

export const replyCardKey = (messageId: string, blockIndex: number) => `${messageId}:${blockIndex}`;

function firstText(message: ThreadMessage): string {
  for (const block of message.blocks) if (block.kind === "text") return block.text.trim();
  return "";
}

/**
 * Resolves every reply card in `messages` (oldest first):
 * - cards sharing an `id` collapse into the first one's position, showing the latest settled
 *   data (a still-pending update never replaces a finished card with a skeleton);
 * - the person's next message after a card is its answer;
 * - follow-up chips belong to the latest answer only: any `follow_ups` card that is not in the
 *   thread's final message (the person replied, or the Muse wrote more) is hidden.
 */
export function resolveReplyCards(
  messages: readonly ThreadMessage[],
): Map<string, ResolvedReplyCard> {
  const latest = new Map<string, ReplyCardBlock>();
  for (const message of messages) {
    for (const block of message.blocks) {
      if (block.kind !== "reply_card" || !block.id) continue;
      const current = latest.get(block.id);
      if (!current || !block.pending || current.pending) latest.set(block.id, block);
    }
  }

  const lastMessage = messages.at(-1);
  const resolved = new Map<string, ResolvedReplyCard>();
  const seen = new Set<string>();
  const unanswered: string[] = [];
  const answers = new Map<string, string>();
  for (const message of messages) {
    if (message.role === "user") {
      const text = firstText(message);
      for (const key of unanswered.splice(0)) answers.set(key, text);
    }
    message.blocks.forEach((block, index) => {
      if (block.kind !== "reply_card") return;
      const key = replyCardKey(message.id, index);
      if (message.role !== "user") unanswered.push(key);
      if (block.card === "follow_ups") {
        resolved.set(key, { block: message === lastMessage ? block : null });
      } else if (!block.id) {
        resolved.set(key, { block });
      } else if (seen.has(block.id)) {
        resolved.set(key, { block: null });
      } else {
        seen.add(block.id);
        resolved.set(key, { block: latest.get(block.id) ?? block });
      }
    });
  }
  for (const [key, answer] of answers) {
    const entry = resolved.get(key);
    if (entry) resolved.set(key, { ...entry, answer });
  }
  return resolved;
}

/**
 * Messages that hold nothing but follow-up chips the thread does not show right now (not the
 * final message, or the Muse is working). A transcript drops these rows entirely: no empty
 * bubble, no gutter face, no hover actions (Copy has no text to copy).
 */
export function hiddenFollowUpMessageIds(
  messages: readonly ThreadMessage[],
  running: boolean,
): Set<string> {
  const last = messages.at(-1);
  const hidden = new Set<string>();
  for (const message of messages) {
    const onlyChips =
      message.blocks.length > 0 &&
      message.blocks.every((block) => block.kind === "reply_card" && block.card === "follow_ups");
    if (onlyChips && (running || message !== last)) hidden.add(message.id);
  }
  return hidden;
}
