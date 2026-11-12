import type { FamilyEvent, ProductEvent } from "@superchat/contracts";

type Notification = Pick<ProductEvent, "id" | "type" | "threadId" | "seq" | "botId" | "payload">;

export interface ReplyAlertDeps {
  botId: string;
  botName: string;
  /** The Conversation's chat id; replies anywhere else in the family raise nothing. */
  conversationId: string | null;
  /** The Muse's "notify when it finishes" setting. */
  enabled: boolean;
  /** The person is looking at the app right now. */
  focused: () => boolean;
  /** The text of the reply, for the notification body; empty when it cannot be read. */
  readText: (itemId: string) => Promise<string>;
  notify: (event: Notification, botName: string, enabled: boolean) => void;
}

/**
 * A reply landed in the Conversation (`messageDone` on the family stream):
 * unless the person is looking, raise the browser notification (`shouldNotifyBrowser`:
 * permission, once per message, not while the window is focused). The unread dot is the engine's
 * own read state.
 */
export async function alertForReply(event: FamilyEvent, deps: ReplyAlertDeps): Promise<void> {
  if (event.type !== "messageDone" || event.chatId !== deps.conversationId) return;
  if (deps.focused()) return;
  const text = await deps.readText(event.itemId).catch(() => "");
  deps.notify(
    {
      id: `${event.chatId}:${event.itemId}`,
      type: "thread.message.created",
      threadId: event.chatId,
      seq: 1,
      botId: deps.botId,
      payload: { role: "bot", blocks: text ? [{ kind: "text", text }] : [] },
    },
    deps.botName,
    deps.enabled,
  );
}
