import type { Bot, ProductEvent } from "@superchat/contracts";
import { useEffect, useRef } from "react";
import { watchFamily } from "../../../lib/family-stream";
import { rpc } from "../../../lib/rpc";
import { alertForReply } from "./replyAlerts";

type Notify = (
  event: Pick<ProductEvent, "id" | "type" | "threadId" | "seq" | "botId" | "payload">,
  subscribedThreadId: string | undefined,
  initialCursor: number,
  streamReady: boolean,
  botName: string,
  enabled: boolean,
) => void;

/** Browser notification for the Muse's replies, driven by the family stream. */
export function useReplyAlerts({
  bot,
  conversationId,
  notifyBrowserForEvent,
}: {
  bot: Bot | undefined;
  conversationId: string | null;
  notifyBrowserForEvent: Notify;
}) {
  const latest = useRef({ bot, conversationId, notifyBrowserForEvent });
  latest.current = { bot, conversationId, notifyBrowserForEvent };
  const botId = bot?.id;
  useEffect(() => {
    if (!botId) return;
    return watchFamily(botId, (event) => {
      const { bot, conversationId, notifyBrowserForEvent } = latest.current;
      if (!bot) return;
      void alertForReply(event, {
        botId: bot.id,
        botName: bot.name,
        conversationId,
        enabled: true,
        focused: () => document.visibilityState === "visible" && document.hasFocus(),
        readText: async (itemId) => {
          const page = await rpc.chats.transcript({ botId: bot.id });
          const reply = page.messages.find((message) => message.id === itemId);
          return (reply?.blocks ?? [])
            .map((block) => (block.kind === "text" ? block.text : ""))
            .filter(Boolean)
            .join("\n");
        },
        notify: (notification, name, enabled) =>
          notifyBrowserForEvent(notification, notification.threadId, 0, true, name, enabled),
      });
    });
  }, [botId]);
}
