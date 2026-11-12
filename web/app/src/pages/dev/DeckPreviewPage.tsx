import { useEffect, useState } from "react";
import sample from "../../features/artifacts/editor/html/sample-deck.txt?raw";
import { DeckViewer } from "../../features/decks/DeckViewer";
import { setDeckThemeSources } from "../../lib/deck-themes";

const NAME = "sample.deck.html";

type DevWindow = Window & {
  __deckEdits?: unknown[];
  /** Replaces the page's deck source (a test loads a deck the engine's kit built). */
  __deckSetHtml?: (html: string) => void;
  /** The engine's theme dictionary, when a test provides it. */
  __deckThemes?: () => Promise<{ themes: never[]; defaultTheme: string }>;
  /** The theme id the engine reads from a deck's source (null when hand-made). */
  __deckTheme?: (html: string) => Promise<string | null>;
  /** What the engine would answer to a `set-theme` patch: the restyled source. */
  __deckRestyle?: (html: string, theme: string) => Promise<string>;
  /** A theme's sample deck (the engine's kit builds it), for the theme preview. */
  __deckThemeSample?: (themeId: string) => Promise<string>;
};

/**
 * Dev-only route (`/dev/deck`, gated by `import.meta.env.DEV` in App.tsx): the deck in the
 * artifact editor shell, selectable, the engine replaced by a recorder
 * (`window.__deckEdits`), so selecting, editing and switching themes can be driven in a real
 * browser without a session or engine. A test may hand it a kit-built deck, the theme dictionary
 * and a restyle function (`window.__deckSetHtml`, `__deckThemes`, `__deckRestyle`).
 */
export function DeckPreviewPage() {
  const [version, setVersion] = useState(1);
  const [html, setHtml] = useState(sample);
  // Read at call time: a run exposes the function after the page has loaded.
  useState(() =>
    setDeckThemeSources({
      sample: async (id) => {
        const dev = window as DevWindow;
        if (!dev.__deckThemeSample) throw new Error("no sample deck in this run");
        return dev.__deckThemeSample(id);
      },
    }),
  );
  useEffect(() => {
    const dev = window as DevWindow;
    dev.__deckSetHtml = (next) => {
      setHtml(next);
      setVersion(1);
    };
    return () => {
      dev.__deckSetHtml = undefined;
    };
  }, []);
  return (
    <div className="flex h-dvh justify-center bg-background p-2">
      <section
        data-testid="dev-panel"
        className="flex min-h-0 w-[min(84vw,1600px)] flex-col overflow-hidden rounded-[18px] border border-border bg-card"
      >
        <span data-testid="dev-version" className="sr-only">
          v{version}
        </span>
        <div className="flex min-h-0 flex-1 flex-col">
          <DeckViewer
            html={html}
            title={NAME}
            artifact={{ id: `v${version}`, version }}
            themesSource={async () => {
              const dev = window as DevWindow;
              if (dev.__deckThemes) return dev.__deckThemes();
              return { themes: [], defaultTheme: "" };
            }}
            themeSource={async () => {
              const dev = window as DevWindow;
              return dev.__deckTheme ? dev.__deckTheme(html) : null;
            }}
            editSource={async (input) => {
              const dev = window as DevWindow;
              dev.__deckEdits = [...(dev.__deckEdits ?? []), input];
              const theme = input.patches.find((patch) => patch.kind === "set-theme");
              if (theme?.kind === "set-theme" && dev.__deckRestyle) {
                setHtml(await dev.__deckRestyle(html, theme.theme));
              }
              const full = input.patches.find((patch) => patch.kind === "set-full-source");
              if (full?.kind === "set-full-source") setHtml(full.source);
              setVersion(input.baseVersion + 1);
              return { id: `v${input.baseVersion + 1}`, version: input.baseVersion + 1 };
            }}
          />
        </div>
      </section>
    </div>
  );
}
