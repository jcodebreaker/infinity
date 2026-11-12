import { useCallback, useRef, useState } from "react";

/** The transcript's scroll element and the one-shot request to bring a message into view (a
 * search hit). */
export function useTranscriptScroll() {
  const messageScroll = useRef<HTMLDivElement>(null);
  const jumpGeneration = useRef(0);
  const [scrollRequest, setScrollRequest] = useState<{
    messageId: string;
    nonce: number;
  } | null>(null);
  const clearScrollRequest = useCallback(() => setScrollRequest(null), []);
  /** Scrolls to a row already rendered (or about to be): the Muse's transcript loads it itself. */
  const scrollToMessage = useCallback((messageId: string) => {
    jumpGeneration.current += 1;
    setScrollRequest({ messageId, nonce: jumpGeneration.current });
  }, []);
  return { messageScroll, scrollRequest, clearScrollRequest, scrollToMessage };
}
