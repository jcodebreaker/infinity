import type { Bot, ProductEvent } from "@superchat/contracts";
import { type MutableRefObject, useCallback, useRef } from "react";
import {
  deliverBrowserNotification as deliverNativeBrowserNotification,
  shouldNotifyBrowser,
} from "../../../lib/browser-notifications";

export type PendingBrowserNotification = {
  event: Pick<ProductEvent, "id" | "type" | "threadId" | "botId" | "payload">;
  botId: string;
  botName: string;
};

/** Native browser notifications for finished runs, held until permission is granted. */
export function useBrowserNotifications(botsRef: MutableRefObject<Bot[]>) {
  const notifiedBrowserEvents = useRef(new Set<string>());
  const pendingBrowserNotifications = useRef(new Map<string, PendingBrowserNotification>());
  const deliverBrowserNotification = useCallback((pending: PendingBrowserNotification): boolean => {
    const currentBot = botsRef.current.find((bot) => bot.id === pending.botId);
    if (!currentBot || typeof Notification === "undefined") return true;
    const result = deliverNativeBrowserNotification(
      pending.event,
      currentBot.name || pending.botName,
      {
        enabled: true,
        pageVisible: document.visibilityState === "visible",
        windowFocused: document.hasFocus(),
        permission: Notification.permission,
        notifiedEventIds: notifiedBrowserEvents.current,
        show: (title, body, tag) => new Notification(title, { body, tag }),
      },
    );
    return result !== "pending";
  }, []);
  const flushPendingBrowserNotifications = useCallback(() => {
    for (const [threadId, pending] of pendingBrowserNotifications.current) {
      if (deliverBrowserNotification(pending)) {
        pendingBrowserNotifications.current.delete(threadId);
      }
    }
  }, [deliverBrowserNotification]);
  const notifyBrowserForEvent = useCallback(
    (
      event: Pick<ProductEvent, "id" | "type" | "threadId" | "seq" | "botId" | "payload">,
      subscribedThreadId: string | undefined,
      initialCursor: number,
      streamReady: boolean,
      botName: string,
      enabled: boolean,
    ) => {
      const botId = event.botId;
      if (typeof botId !== "string") return;
      const eligible = shouldNotifyBrowser(event, {
        subscribedThreadId: subscribedThreadId ?? "",
        initialCursor,
        streamReady,
        pageVisible: document.visibilityState === "visible",
        windowFocused: document.hasFocus(),
        permission: "granted",
        notifiedEventIds: notifiedBrowserEvents.current,
      });
      if (!eligible || !enabled) return;
      const pending = { event, botId, botName } satisfies PendingBrowserNotification;
      if (typeof Notification === "undefined" || Notification.permission === "denied") return;
      if (Notification.permission === "default") {
        pendingBrowserNotifications.current.set(event.threadId, pending);
        return;
      }
      if (deliverBrowserNotification(pending)) {
        pendingBrowserNotifications.current.delete(event.threadId);
      } else {
        pendingBrowserNotifications.current.set(event.threadId, pending);
      }
    },
    [deliverBrowserNotification],
  );
  return { flushPendingBrowserNotifications, notifyBrowserForEvent };
}
