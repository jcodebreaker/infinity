import type { ThreadMessage, ThreadMessagePage } from "@superchat/contracts";

/** The newest transcript page merged into what is loaded. Older pages already loaded stay when
 * the new page overlaps them; otherwise (a long gap) the new page stands alone. */
export function mergeNewestTranscriptPage(
  loaded: { messages: ThreadMessage[]; olderCursor: string | null } | null,
  page: ThreadMessagePage,
): { threadId: string; messages: ThreadMessage[]; olderCursor: string | null } {
  const newest = page.messages;
  const fresh = {
    threadId: page.threadId,
    messages: newest,
    olderCursor: page.olderItemCursor ?? null,
  };
  const first = newest[0];
  if (!loaded || !first) return fresh;
  const overlap = loaded.messages.findIndex((message) => message.id === first.id);
  if (overlap === -1) return fresh;
  return {
    threadId: page.threadId,
    messages: [...loaded.messages.slice(0, overlap), ...newest],
    olderCursor: loaded.olderCursor,
  };
}

/** How many messages of the person's the transcript holds. */
export const userCount = (messages: readonly ThreadMessage[] | null) =>
  messages?.filter((message) => message.role === "user").length ?? 0;

/** What the person just sent, shown before the engine has recorded it. */
export function optimisticMessage(chatId: string, text: string): ThreadMessage {
  return {
    id: "optimistic",
    threadId: chatId,
    seq: Number.MAX_SAFE_INTEGER,
    role: "user",
    blocks: [{ kind: "text", text }],
    createdAt: new Date().toISOString(),
  };
}

/** A quoted passage as a markdown blockquote above what the person wrote. */
export function quotedMessageText(quote: string, text: string): string {
  const block = quote
    .trim()
    .split("\n")
    .map((line) => `> ${line}`.trimEnd())
    .join("\n");
  return text ? `${block}\n\n${text}` : block;
}
