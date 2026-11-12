import type { ThreadMessage } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AskPreviewsProvider } from "../../components/cards/context";
import { deriveWorkState, IDLE_TURN, transcriptWork } from "../../features/activity";
import { DeckThemeChoice } from "../../features/decks";
import type { ArtifactTarget } from "../../lib/artifact-open";
import { setDeckThemeSources } from "../../lib/deck-themes";
import { Transcript } from "../app/conversation/Transcript";
import { type CardsScenario, replyTo, seedMessages } from "./cards-fixture";
import { DEV_BOT_ID, DEV_MUSE_COLOR, DEV_MUSE_NAME } from "./dev-muse";

const noop = () => undefined;
const ASK_PREVIEWS = { "deck-theme": DeckThemeChoice };
const noopAsync = async () => undefined;

type DevWindow = Window & {
  /** The engine's theme dictionary, when a test or screenshot run provides it. */
  __deckThemes?: () => Promise<{ themes: never[]; defaultTheme: string }>;
  /** A theme's sample deck (the engine's kit builds it), for the theme preview. */
  __deckThemeSample?: (themeId: string) => Promise<string>;
};

/**
 * Dev-only routes (`/dev/clarify`, `/dev/followups`, `/dev/looks`, gated by `import.meta.env.DEV` in App.tsx):
 * the real Conversation transcript with a clarification card or follow-up
 * chips (`/dev/looks`: the deck look question; a run provides the theme dictionary and sample
 * decks through `window.__deckThemes` / `__deckThemeSample`). Clicking sends the text as the
 * person's message, the fixture "Muse" works for a moment
 * (`running`: chips hidden) and then answers. `?theme=light|dark` forces a theme.
 */
export function CardsPreviewPage({ scenario }: { scenario: CardsScenario }) {
  const [messages, setMessages] = useState<ThreadMessage[]>(() => seedMessages(scenario));
  const [running, setRunning] = useState(false);
  const work = useMemo(() => transcriptWork(messages), [messages]);
  const workState = useMemo(
    () =>
      deriveWorkState({
        turn: { ...IDLE_TURN, running },
        sending: false,
        stopping: false,
        activities: [],
        turnStartedAt: work.runningSince ?? null,
      }),
    [running, work],
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const artifactTarget = useMemo<ArtifactTarget>(() => ({ botId: DEV_BOT_ID }), []);

  // Read at call time: a run exposes the functions after the page has loaded.
  useState(() => {
    const dev = window as DevWindow;
    setDeckThemeSources({
      themes: async () =>
        dev.__deckThemes ? dev.__deckThemes() : { themes: [], defaultTheme: "" },
      sample: async (id) => {
        if (!dev.__deckThemeSample) throw new Error("no sample deck in this run");
        return dev.__deckThemeSample(id);
      },
    });
  });

  useEffect(() => {
    const theme = new URLSearchParams(window.location.search).get("theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    return () => window.clearTimeout(timer.current);
  }, []);

  const send = useCallback(
    (said: string) => {
      setMessages((current) => [
        ...current,
        {
          id: `person-${current.length}`,
          threadId: "cards",
          seq: current.length + 1,
          role: "user",
          blocks: [{ kind: "text", text: said }],
          createdAt: new Date().toISOString(),
        },
      ]);
      setRunning(true);
      timer.current = window.setTimeout(() => {
        setMessages((current) => [...current, ...replyTo(scenario, said)]);
        setRunning(false);
      }, 1200);
    },
    [scenario],
  );

  return (
    <div className="flex h-screen bg-background p-2">
      <main
        className={cn(
          "relative mx-auto flex min-h-0 w-full max-w-[860px] min-w-0 flex-1 flex-col overflow-hidden",
          "border border-line bg-panel backdrop-blur-xl md:rounded-[18px]",
        )}
      >
        <div className="flex h-14 shrink-0 items-center border-b border-line px-6">
          <h1 className="text-[15px] font-semibold">Conversation</h1>
        </div>
        <AskPreviewsProvider value={ASK_PREVIEWS}>
          <Transcript
            botDisplayName={DEV_MUSE_NAME}
            museFace={{ color: DEV_MUSE_COLOR, identity: DEV_BOT_ID }}
            museRuns={[]}
            scrollRef={scrollRef}
            scrollRequest={null}
            onScrollRequestHandled={noop}
            artifactTarget={artifactTarget}
            messages={messages}
            olderCursor={null}
            loadingOlder={false}
            work={work}
            workState={workState}
            onLoadOlder={noop}
            onAnswer={noopAsync}
            onSendCard={send}
          />
        </AskPreviewsProvider>
      </main>
    </div>
  );
}
