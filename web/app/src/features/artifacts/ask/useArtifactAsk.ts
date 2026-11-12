import { useLingui } from "@lingui/react/macro";
import type { ThreadMessage } from "@superchat/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import { watchFamily } from "../../../lib/family-stream";
import { rpc } from "../../../lib/rpc";

/** Where an ask goes and which saved file it is about. */
export type AskArtifact = {
  id: string;
  name: string;
  version: number;
  /** The Muse that owns the artifact; its Conversation receives the request. */
  botId: string;
  /** The file's studio chat (ADR 0024): the request goes there instead of the Conversation. */
  chatId?: string;
};

/** The studio's turn (ADR 0024), owned by its chat: the ask follows it instead of the stream. */
export type AskTurn = {
  running: boolean;
  /** A request was sent: the turn is running from now. */
  onSent: () => void;
};

export type AskStatus =
  | { kind: "idle" }
  | { kind: "sending"; asked: string }
  | { kind: "editing"; asked: string }
  | { kind: "updated"; asked: string; version: number }
  | { kind: "replied"; asked: string; reply: string }
  | { kind: "failed"; asked: string; message: string };

/** How long a turn that ended may still be followed by its version landing (the views poll). */
const VERSION_GRACE_MS = 6000;
const REPLY_MAX = 220;

/** The plain text of the newest reply in a transcript page. */
export function lastReplyText(messages: readonly ThreadMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message?.role !== "bot") continue;
    const text = message.blocks
      .map((block) => (block.kind === "text" ? block.text : ""))
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
    if (text) return text.length > REPLY_MAX ? `${text.slice(0, REPLY_MAX - 1)}…` : text;
  }
  return "";
}

/**
 * Asks the Muse to change an artifact in place, from wherever the artifact is open: the kind's
 * context block (what is selected, the hard scope) goes ahead of the person's words straight to the
 * Muse's Conversation, or to the file's studio chat. The status follows the turn: editing until a
 * newer version of the artifact is on screen (`version` grows), or, when the turn ends without
 * one, the Muse's last reply. In a studio the turn is the studio's (`turn`); otherwise the
 * Conversation's end is read from the family stream.
 */
export function useArtifactAsk({
  artifact,
  contextBlock,
  turn,
}: {
  artifact: AskArtifact | null;
  /** The kind's request block for the current selection; "" for none. */
  contextBlock: () => string | Promise<string>;
  turn?: AskTurn;
}) {
  const { t } = useLingui();
  const [status, setStatus] = useState<AskStatus>({ kind: "idle" });
  const statusRef = useRef(status);
  statusRef.current = status;
  const base = useRef(0);
  const botId = artifact?.botId;
  const chatId = artifact?.chatId;
  const artifactId = artifact?.id ?? "";
  const version = artifact?.version ?? 0;
  const onSent = turn?.onSent;
  const studioRunning = turn?.running ?? false;
  const following = !!turn;

  // A newer version on screen while asking is the answer. After a reply, a later version may be
  // anyone's (a Helper's, a hand edit): the reply is stale, so the line clears.
  useEffect(() => {
    const current = statusRef.current;
    if ((current.kind === "editing" || current.kind === "sending") && version > base.current) {
      setStatus({ kind: "updated", asked: current.asked, version });
    } else if (current.kind === "replied" && version > base.current) {
      setStatus({ kind: "idle" });
    }
  }, [version]);

  // The turn ended: if no version follows soon, show why with the Muse's reply. A studio's turn
  // ends when its chat says it stopped running; the Conversation's on the stream's turnDone.
  const [ended, setEnded] = useState(0);
  const wasRunning = useRef(studioRunning);
  useEffect(() => {
    if (wasRunning.current && !studioRunning) setEnded((n) => n + 1);
    wasRunning.current = studioRunning;
  }, [studioRunning]);
  useEffect(() => {
    if (!botId || following || status.kind !== "editing") return;
    return watchFamily(botId, (event) => {
      if (event.type === "turnDone" && event.chatId === botId) setEnded((n) => n + 1);
    });
  }, [botId, following, status.kind]);
  useEffect(() => {
    if (!botId || ended === 0 || statusRef.current.kind !== "editing") return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      // A version saved but not on screen yet (the view's poll is paused in a hidden tab):
      // keep waiting for it rather than reporting no change.
      const newest = await rpc.artifacts
        .listVersions({ familyId: artifactId })
        .then((list) => list[0]?.version ?? 0)
        .catch(() => 0);
      if (cancelled || newest > base.current) return;
      const reply = await rpc.chats
        .transcript(chatId ? { botId, chatId } : { botId })
        .then((page) => lastReplyText(page.messages))
        .catch(() => "");
      const current = statusRef.current;
      if (cancelled || current.kind !== "editing") return;
      setStatus({ kind: "replied", asked: current.asked, reply });
    }, VERSION_GRACE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [botId, chatId, artifactId, ended]);

  const send = useCallback(
    async (words: string, savedVersion?: number) => {
      const asked = words.trim();
      if (!botId || !asked) return false;
      // The newest saved version (one just saved by hand may not be on screen yet) is the base.
      base.current = Math.max(version, savedVersion ?? 0);
      setStatus({ kind: "sending", asked });
      try {
        const block = await contextBlock();
        const text = block ? `${block}\n\n${asked}` : asked;
        await rpc.chats.send(chatId ? { botId, chatId, text } : { botId, text });
        onSent?.();
        setStatus((current) => (current.kind === "sending" ? { kind: "editing", asked } : current));
        return true;
      } catch (error) {
        setStatus({
          kind: "failed",
          asked,
          message: error instanceof Error ? error.message : "",
        });
        return false;
      }
    },
    [botId, chatId, contextBlock, version, onSent],
  );

  const stop = useCallback(async () => {
    if (!botId) return;
    const asked = "asked" in statusRef.current ? statusRef.current.asked : "";
    setStatus({ kind: "idle" });
    const delivered = await rpc.chats
      .interrupt(chatId ? { botId, chatId } : { botId })
      .then((answer) => answer.delivered)
      .catch(() => false);
    // Said the way the composer says it: the turn may still be running.
    if (!delivered) setStatus({ kind: "failed", asked, message: t`Couldn't stop` });
  }, [botId, chatId, t]);

  const dismiss = useCallback(() => setStatus({ kind: "idle" }), []);

  return { status, send, stop, dismiss, available: !!botId };
}
