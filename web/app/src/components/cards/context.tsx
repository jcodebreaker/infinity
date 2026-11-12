import type { ThreadMessage } from "@superchat/contracts";
import { type ComponentType, createContext, type ReactNode, useContext, useMemo } from "react";
import { type ResolvedReplyCard, replyCardKey, resolveReplyCards } from "./thread";

/** Which ask card option a message answers, sent beside its text (ADR 0025). */
export interface CardAnswer {
  cardId: string;
  optionId: string;
  label: string;
}
/** How a card talks back: the person's next message, through the thread's own send path. */
type CardSend = (text: string, answer?: CardAnswer) => void;
const SendContext = createContext<CardSend | null>(null);
const ThreadContext = createContext<Map<string, ResolvedReplyCard> | null>(null);
/** Whether the Muse is working on this thread; follow-up chips stay hidden meanwhile. */
const RunningContext = createContext(false);

export function ReplyCardSendProvider({ send, children }: { send: CardSend; children: ReactNode }) {
  return <SendContext.Provider value={send}>{children}</SendContext.Provider>;
}

/** How a card puts words in the composer for the person to edit or send (a topic's starters);
 * nothing is sent. */
type CardDraft = (text: string) => void;
const DraftContext = createContext<CardDraft | null>(null);

export function ReplyCardDraftProvider({
  draft,
  children,
}: {
  draft: CardDraft;
  children: ReactNode;
}) {
  return <DraftContext.Provider value={draft}>{children}</DraftContext.Provider>;
}

export const useReplyCardDraft = () => useContext(DraftContext);

/** Wrap a transcript once; its cards then update in place and lock once answered. */
export function ReplyCardThreadProvider({
  messages,
  running = false,
  children,
}: {
  messages: readonly ThreadMessage[];
  /** The Muse is working: follow-up chips are hidden until it settles. */
  running?: boolean;
  children: ReactNode;
}) {
  const resolved = useMemo(() => resolveReplyCards(messages), [messages]);
  const latest = useMemo(() => latestSavedFiles(messages), [messages]);
  return (
    <ThreadContext.Provider value={resolved}>
      <RunningContext.Provider value={running}>
        <LatestFilesContext.Provider value={latest}>{children}</LatestFilesContext.Provider>
      </RunningContext.Provider>
    </ThreadContext.Provider>
  );
}

export type LatestFile = { version: number; artifactId: string };

/** The newest saved version of each deliverable (by file name) anywhere in the thread. */
const LatestFilesContext = createContext<Map<string, LatestFile> | null>(null);

export function latestSavedFiles(messages: readonly ThreadMessage[]): Map<string, LatestFile> {
  const latest = new Map<string, LatestFile>();
  for (const message of messages) {
    for (const block of message.blocks) {
      if (block.kind !== "reply_card" || block.card !== "file" || block.pending) continue;
      const { name, artifactId, version } = block.data as {
        name?: string;
        artifactId?: string;
        version?: number;
      };
      if (!name || !artifactId) continue;
      const current = latest.get(name);
      if (!current || (version ?? 1) >= current.version) {
        latest.set(name, { version: version ?? 1, artifactId });
      }
    }
  }
  return latest;
}

export const useLatestSavedFile = (name: string) => useContext(LatestFilesContext)?.get(name);

/** The File Studio (ADR 0030): the shell owns the route; a card, a Library item or a citation
 * opens a file in it (any version id; `page` opens a PDF at that page). */
export type FileStudioApi = {
  open: (artifactId: string, title?: string, page?: number) => void;
};
const FileStudioContext = createContext<FileStudioApi | null>(null);
export const FileStudioProvider = FileStudioContext.Provider;
export const useFileStudio = () => useContext(FileStudioContext);

/** The Muse whose chat this is; cards that talk to the API on the person's behalf need it. */
const BotIdContext = createContext<string | null>(null);

export function ReplyCardBotProvider({
  botId,
  children,
}: {
  botId: string | undefined;
  children: ReactNode;
}) {
  return <BotIdContext.Provider value={botId || null}>{children}</BotIdContext.Provider>;
}

export const useReplyCardBotId = () => useContext(BotIdContext);

export const useReplyCardRunning = () => useContext(RunningContext);

export const useReplyCardSend = () => useContext(SendContext);

export function useResolvedReplyCard(
  messageId: string,
  blockIndex: number,
): ResolvedReplyCard | undefined {
  return useContext(ThreadContext)?.get(replyCardKey(messageId, blockIndex));
}

/** What a capability's answer tiles get for an ask card whose options preview its kind. */
export interface AskPreviewProps {
  /** Each answer with the id of what it previews (a deck theme id, for `deck-theme`). */
  options: readonly { id: string; label: string; previewId: string }[];
  /** The label the person picked (their reply), or null. */
  chosen: string | null;
  locked: boolean;
  onPick: (label: string) => void;
}

/** `preview kind -> tiles` (decks: `deck-theme`), filled by the Shell so cards import no
 * capability. An ask card whose kind has no tiles here keeps its plain buttons. */
const AskPreviewsContext = createContext<Readonly<Record<string, ComponentType<AskPreviewProps>>>>(
  {},
);
export const AskPreviewsProvider = AskPreviewsContext.Provider;
export const useAskPreviews = () => useContext(AskPreviewsContext);
