import type { StudioPrelude, ThreadMessage } from "@superchat/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import { useFamilyLive, watchFamily } from "../../../lib/family-stream";
import { rpc } from "../../../lib/rpc";
import { useVisiblePoll } from "../../../lib/use-visible-poll";
import { notifyArtifactsChanged } from "../artifactChanges";

/** How often the chat is re-read while a turn runs and the family stream is down. */
const CHAT_POLL_MS = 3000;
/** How often an open studio tells the engine it is still open (its runner stays warm). */
export const OPEN_SIGNAL_MS = 5 * 60_000;

export type StudioChat = {
  /** The file's studio session, once found or made. */
  sessionId: string | null;
  /** Its transcript, oldest first. */
  messages: ThreadMessage[];
  /** What made the file's versions in Super Chat (ADR 0030), from the same response as the
   * session; empty when nothing did, or from an engine without it. */
  prelude: StudioPrelude[];
  /** The transcript has been read once: the thread paints from it whole, never in pieces. */
  loaded: boolean;
  /** A studio turn is running (in this tab or another): the studio's one running state. */
  running: boolean;
  /** A request was just sent from this tab: running until the turn says otherwise. */
  sent: () => void;
  /** The session could not be opened. */
  failed: boolean;
  /** The engine's words while a studio turn waits for a slot ("Finishing another file
   * first"), from the session's status; null otherwise. */
  blockedOn: string | null;
};

/**
 * The file's studio chat (ADR 0024): finds or makes its session (`artifacts.chat`, which never
 * wakes the host), reads its transcript, and follows the Muse's family stream (no stream of its
 * own) for that session's status (running, waiting), its messages and saves of the file
 * (`onSaved`).
 */
export function useStudioChat({
  artifactId,
  botId,
  groupId,
  onSaved,
}: {
  /** Any version of the file. */
  artifactId: string;
  botId: string | null;
  groupId: string | null;
  onSaved?: () => void;
}): StudioChat {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [messages, setMessages] = useState<ThreadMessage[]>([]);
  const [prelude, setPrelude] = useState<StudioPrelude[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [running, setRunning] = useState(false);
  const [blockedOn, setBlockedOn] = useState<string | null>(null);
  // The family stream carries the session's status; while it is down, a running turn is polled.
  const live = useFamilyLive(botId);
  const onSavedRef = useRef(onSaved);
  onSavedRef.current = onSaved;

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    void rpc.artifacts
      .chat({ artifactId })
      .then((opened) => {
        if (cancelled) return;
        setPrelude(opened.prelude);
        setSessionId(opened.sessionId);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
    // One session per file, whichever version was opened: not refetched when the version moves.
  }, [artifactId]);

  // The studio is open: the engine keeps its runner warm while it hears this (ADR 0024
  // "Runner").
  useEffect(() => {
    const signal = () => void rpc.artifacts.chatOpen({ artifactId }).catch(() => {});
    signal();
    const timer = window.setInterval(signal, OPEN_SIGNAL_MS);
    return () => window.clearInterval(timer);
  }, [artifactId]);

  const read = useCallback(async () => {
    if (!botId || !sessionId) return;
    const page = await rpc.chats.transcript({ botId, chatId: sessionId }).catch(() => null);
    setLoaded(true);
    if (!page) return;
    setMessages(page.messages);
    setRunning(page.running === true);
  }, [botId, sessionId]);

  useEffect(() => {
    void read();
  }, [read]);
  useVisiblePoll(read, CHAT_POLL_MS, !!botId && !!sessionId && running && !live);

  useEffect(() => {
    if (!botId || !sessionId) return;
    return watchFamily(botId, (event) => {
      if (
        (event.type === "messageDone" ||
          event.type === "messageDelivery" ||
          event.type === "turnDone") &&
        event.chatId === sessionId
      ) {
        if (event.type === "turnDone") {
          setRunning(false);
          setBlockedOn(null);
        }
        void read();
      } else if (event.type === "chatStatus" && event.chatId === sessionId) {
        // Running or not, and `blockedOn` while a turn waits behind the host's studio cap.
        const busy = event.status === "running";
        setRunning(busy);
        setBlockedOn(busy ? event.blockedOn : null);
      } else if (event.type === "artifactsChanged" && event.groupId === groupId) {
        onSavedRef.current?.();
        // The card and the Library under the studio follow the new version too.
        notifyArtifactsChanged();
      } else if (event.type === "open") {
        void read();
      }
    });
  }, [botId, sessionId, groupId, read]);

  const sent = useCallback(() => setRunning(true), []);

  return { sessionId, messages, prelude, loaded, running, sent, failed, blockedOn };
}
