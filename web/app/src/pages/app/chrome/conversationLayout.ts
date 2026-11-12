/** The Conversation's two layouts: a start page while it is empty, the thread once it isn't. */
export type ConversationLayout = "start" | "thread";

/**
 * Whether the Conversation shows its start page (the old design notes "Conversation"): the
 * Muse's own Conversation, loaded, with no messages and nothing running. Anything else (still
 * loading, a first message on its way) is the thread with the composer docked.
 */
export function conversationLayout(input: {
  hasMuse: boolean;
  /** False until the first transcript page has loaded. */
  loaded: boolean;
  messageCount: number;
  running: boolean;
}): ConversationLayout {
  return input.hasMuse && input.loaded && input.messageCount === 0 && !input.running
    ? "start"
    : "thread";
}
